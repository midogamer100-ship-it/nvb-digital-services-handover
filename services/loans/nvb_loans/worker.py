"""loans-worker: background summary of AUTHORISED core data for a submitted application.

Reads only statement manifests (adapter scope ``summaries:read``: balances and totals, never the
individual transactions) for the applicant's selected account over the last full months, and
computes a transparent, integer-only indicator. The output is labelled synthetic/demo-only and
is advisory: a human reviewer and a separate approver take the workflow decision.

Same reliability model as the statements worker: at-least-once delivery, conditional claim with
a lease, completion conditional on the lease, ack only after the DB commit, bounded retries with
backoff, non-zero exit without ack on unexpected failures, bounded finite runs (``--drain``).
The summary is a pure function of the task's inputs, so a duplicate delivery is harmless.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import sys
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from prometheus_client import Counter
from sqlalchemy import and_, or_, update
from sqlalchemy.orm import Session, sessionmaker

from nvb_loans.config import Settings, get_settings
from nvb_loans.db import build_engine, build_sessionmaker
from nvb_loans.models import TASK_TERMINAL, LoanApplication, SummaryTask, utcnow
from nvb_platform.adapter_client import AdapterCallError, AdapterClient
from nvb_platform.broker import Broker, Delivery
from nvb_platform.logging_setup import configure_logging, request_id_var

log = logging.getLogger("loans.worker")
TASKS = Counter("loans_worker_tasks_total", "Summary task outcomes", ["outcome"])
MODEL = "synthetic-demo-v1"
NON_RETRYABLE = {"not_found": "authorization_revoked", "invalid_subject": "invalid_request",
                 "invalid_period": "invalid_request", "core_integrity_error": "core_integrity_error",
                 "too_large": "too_large", "insufficient_scope": "worker_misconfigured",
                 "invalid_client": "worker_misconfigured"}


class Crash(Exception):
    """Test hook."""


def last_full_months(now: datetime, tz_name: str, months: int, settle_seconds: int) -> list[tuple[str, datetime, datetime]]:
    """[(label, start_utc, end_utc)] of the most recent ``months`` closed calendar months (oldest first)."""
    tz = ZoneInfo(tz_name)
    local = (now - timedelta(seconds=settle_seconds)).astimezone(tz)
    year, month = local.year, local.month       # first day of the current local month = end of the last full one
    out = []
    for _ in range(months):
        end_local = datetime(year, month, 1, tzinfo=tz)
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
        start_local = datetime(year, month, 1, tzinfo=tz)
        out.append((f"{year:04d}-{month:02d}", start_local.astimezone(UTC), end_local.astimezone(UTC)))
    return list(reversed(out))


def score(*, amount_minor: int, term_months: int, declared_income_minor: int, months: list[dict]) -> dict:
    """Integer-only demo indicator. Not a credit model; no float arithmetic on money."""
    n = len(months)
    avg_credits = sum(m["credit_total_minor"] for m in months) // n
    avg_debits = sum(m["debit_total_minor"] for m in months) // n
    closing = months[-1]["closing_balance_minor"]
    installment = -(-amount_minor // term_months)                      # ceiling division, no interest (demo)
    net_inflow = avg_credits - avg_debits
    # installment as basis points of the average monthly credits (10000 = 100%)
    burden_bps = (installment * 10000 // avg_credits) if avg_credits > 0 else None
    if burden_bps is None:
        band = "insufficient_history"
    elif burden_bps <= 3000 and net_inflow >= 0:
        band = "lower_indicative_risk"
    elif burden_bps <= 5000:
        band = "medium_indicative_risk"
    else:
        band = "higher_indicative_risk"
    return {"model": MODEL, "label": "SYNTHETIC DEMO SCORE - not a credit decision", "band": band,
            "installment_minor": installment, "installment_to_credits_bps": burden_bps,
            "average_monthly_credits_minor": avg_credits, "average_monthly_debits_minor": avg_debits,
            "average_net_inflow_minor": net_inflow, "latest_closing_balance_minor": closing,
            "declared_income_minor": declared_income_minor,
            "months": [{"period": m["period"], "credit_total_minor": m["credit_total_minor"],
                        "debit_total_minor": m["debit_total_minor"],
                        "closing_balance_minor": m["closing_balance_minor"], "entry_count": m["entry_count"]}
                       for m in months]}


class Worker:
    def __init__(self, settings: Settings, sm: sessionmaker[Session], broker: Broker, adapter: AdapterClient,
                 worker_id: str | None = None, crash_at: str | None = None) -> None:
        self.s, self.sm, self.broker, self.adapter = settings, sm, broker, adapter
        self.worker_id = worker_id or settings.worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.crash_at = crash_at
        self.stopping = False

    def _maybe_crash(self, point: str) -> None:
        if self.crash_at == point:
            raise Crash(point)

    def run(self, *, drain: bool, max_messages: int | None = None, max_runtime_seconds: float | None = None) -> int:
        handled, started = 0, time.monotonic()
        idle_since = started
        while not self.stopping:
            if max_messages and handled >= max_messages:
                return 0
            if max_runtime_seconds and time.monotonic() - started >= max_runtime_seconds:
                return 0
            d = self.broker.get(self.s.queue_name, timeout=1.0)
            if d is None:
                if drain and time.monotonic() - idle_since >= self.s.idle_exit_seconds:
                    log.info("queue idle; exiting", extra={"handled": handled})
                    return 0
                continue
            try:
                self.handle(d)
            except Exception:  # noqa: BLE001 - never ack on unexpected failure
                log.exception("unexpected failure; message requeued, exiting")
                try:
                    self.broker.reject(d, requeue=True)
                except Exception:  # noqa: BLE001
                    pass
                TASKS.labels("crashed").inc()
                return 2
            handled += 1
            idle_since = time.monotonic()
        return 0

    def handle(self, d: Delivery) -> str:
        body = d.body
        if body.get("type") != "loan.summarize" or not isinstance(body.get("task_id"), str):
            log.error("malformed message dead-lettered", extra={"message_id": d.message_id})
            self.broker.reject(d, requeue=False)
            TASKS.labels("dead_lettered").inc()
            return "dead_lettered"
        tid = body["task_id"][:36]
        token = request_id_var.set(f"loan-summary-{tid}")
        try:
            outcome = self._process(tid)
        finally:
            request_id_var.reset(token)
        self._maybe_crash("before_ack")
        self.broker.ack(d)
        TASKS.labels(outcome).inc()
        return outcome

    def _claim(self, db: Session, tid: str) -> SummaryTask | None:
        now = utcnow()
        claimed = db.execute(
            update(SummaryTask)
            .where(SummaryTask.id == tid,
                   or_(and_(SummaryTask.status == "queued",
                            or_(SummaryTask.not_before.is_(None), SummaryTask.not_before <= now)),
                       and_(SummaryTask.status == "running", SummaryTask.lease_expires_at < now)))
            .values(status="running", attempts=SummaryTask.attempts + 1, lease_owner=self.worker_id,
                    lease_expires_at=now + timedelta(seconds=self.s.lease_seconds), updated_at=now, not_before=None))
        if claimed.rowcount != 1:
            db.rollback()
            return None
        db.commit()
        task = db.get(SummaryTask, tid)
        db.commit()
        return task

    def _finish(self, db: Session, task: SummaryTask, **values) -> bool:
        now = utcnow()
        done = db.execute(update(SummaryTask)
                          .where(SummaryTask.id == task.id, SummaryTask.status == "running",
                                 SummaryTask.lease_owner == self.worker_id)
                          .values(updated_at=now, lease_owner=None, lease_expires_at=None, **values))
        db.commit()
        return done.rowcount == 1

    def _process(self, tid: str) -> str:
        with self.sm() as db:
            task = self._claim(db, tid)
            if task is None:
                existing = db.get(SummaryTask, tid)
                db.rollback()
                if existing is None:
                    return "unknown"
                return "already_" + existing.status if existing.status in TASK_TERMINAL else "duplicate"
            self._maybe_crash("after_claim")
            app = db.get(LoanApplication, task.application_id)
            db.rollback()
            if app is None:
                self._finish(db, task, status="failed", error_code="application_missing", completed_at=utcnow())
                return "failed"
            if task.attempts > self.s.max_attempts:
                self._finish(db, task, status="failed", error_code="attempts_exhausted", completed_at=utcnow())
                return "failed"
            try:
                months = []
                for label, start, end in last_full_months(utcnow(), self.s.summary_timezone, self.s.summary_months,
                                                          self.s.settle_seconds):
                    m = self.adapter.manifest(app.account_id, app.owner_subject, start, end, request_id_var.get())
                    months.append({"period": label, **{k: m[k] for k in (
                        "credit_total_minor", "debit_total_minor", "closing_balance_minor", "entry_count")}})
                    self.broker.keepalive()
            except AdapterCallError as exc:
                if not exc.retryable and (exc.code in NON_RETRYABLE or exc.status_code < 500) \
                        and exc.code != "period_not_settled":
                    self._finish(db, task, status="failed", completed_at=utcnow(),
                                 error_code=NON_RETRYABLE.get(exc.code, "invalid_request"))
                    log.warning("summary failed", extra={"code": exc.code})
                    return "failed"
                if task.attempts >= self.s.max_attempts:
                    self._finish(db, task, status="failed", error_code="attempts_exhausted", completed_at=utcnow())
                    return "failed"
                delay = min(self.s.retry_max_seconds, self.s.retry_base_seconds * (2 ** (task.attempts - 1)))
                self._finish(db, task, status="queued", error_code=exc.code,
                             not_before=utcnow() + timedelta(seconds=delay))
                return "retry_scheduled"
            result = score(amount_minor=app.amount_minor, term_months=app.term_months,
                           declared_income_minor=app.monthly_income_minor, months=months)
            self._maybe_crash("before_commit")
            if not self._finish(db, task, status="completed", result=result, error_code=None, completed_at=utcnow()):
                return "lease_lost"
            self._maybe_crash("after_commit")
            return "completed"


def main(argv: list[str] | None = None) -> int:
    from nvb_platform.broker import BrokerUnavailable, RabbitBroker

    p = argparse.ArgumentParser(prog="loans-worker")
    p.add_argument("--drain", action="store_true", help="finite run: exit once the queue is idle (Job mode)")
    p.add_argument("--max-messages", type=int, default=None)
    p.add_argument("--max-runtime-seconds", type=int, default=None)
    args = p.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level, "loans-worker", f"{socket.gethostname()}:{os.getpid()}")
    problems = settings.validate_runtime("worker")
    if problems:
        log.error("invalid configuration", extra={"problems": problems})
        return 78
    try:
        broker = RabbitBroker(settings, verify=(settings.queue_name,))
    except BrokerUnavailable as exc:
        log.error("broker unavailable or queue missing (the relay declares it)", extra={"error": str(exc)})
        return 75
    worker = Worker(settings, build_sessionmaker(build_engine(settings, "nvb-loans-worker")), broker,
                    AdapterClient(settings))

    def stop(*_):
        worker.stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    mm = settings.worker_max_messages if args.max_messages is None else args.max_messages
    mr = settings.worker_max_runtime_seconds if args.max_runtime_seconds is None else args.max_runtime_seconds
    try:
        return worker.run(drain=args.drain, max_messages=mm or None, max_runtime_seconds=mr or None)
    finally:
        broker.close()
        worker.adapter.close()


if __name__ == "__main__":
    sys.exit(main())
