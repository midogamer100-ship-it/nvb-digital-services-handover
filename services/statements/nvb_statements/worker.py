"""statements-worker: claim -> verified dataset -> PDF -> private storage -> DB completion -> ack.

Delivery is at-least-once. Correctness comes from the database, not the broker:

* Claim is one conditional UPDATE (queued & due, or running with an expired lease). A second
  delivery of the same request cannot claim it while a live lease exists, so it is acked.
* The lease is RENEWED by a background heartbeat while a task runs, so a long statement is not
  stolen by the reconciler or a second worker. If the process dies the lease simply expires.
* The object key is deterministic (``statements/<request>/<dataset_id>.pdf``): a retry after a
  crash between upload and DB completion overwrites the same logical file, never a second one.
* Completion is conditional on still owning the lease. Only after it commits is the message acked.
  A crash after commit but before ack causes a redelivery, which sees a terminal state and acks.
* Retryable failures put the request back to ``queued`` with ``not_before`` (capped exponential
  backoff) and ack the message; the reconciler re-enqueues it when due. Non-retryable failures
  (authorization revoked, invalid request, too large) are terminal at once - no infinite retries.
* Unexpected errors (DB/broker down) are never acked: the message is requeued and the process
  exits non-zero so a Job/ScaledJob does not spin. The broker dead-letters a message after
  ``x-delivery-limit`` redeliveries (poison protection).

Execution model (one coherent model for manual Jobs and KEDA ScaledJobs)
-------------------------------------------------------------------------
``python -m nvb_statements.worker --drain`` is a FINITE, BOUNDED run:

* it takes messages one at a time (interactive queue first, then the batch queue);
* it stops taking new messages after ``STATEMENTS_WORKER_MAX_MESSAGES`` messages or
  ``STATEMENTS_WORKER_MAX_RUNTIME_SECONDS`` seconds, whichever comes first;
* it exits 0 when both queues stayed empty for ``STATEMENTS_IDLE_EXIT_SECONDS``;
* on SIGTERM it finishes the message in hand (never abandons it half-acked) and exits 0.

So one Job handles "a bounded group", not one message and not the whole backlog. The KEDA
ScaledJob target (messages per Job) is set to the same number - see docs/WORKER_SCALING.md.
Without ``--drain`` the process keeps polling (Deployment mode); the bounds still apply when set.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import sys
import threading
import time
from datetime import timedelta

from prometheus_client import Counter, Histogram
from sqlalchemy import and_, or_, update
from sqlalchemy.orm import Session, sessionmaker

from nvb_statements.adapter_client import AdapterCallError, AdapterClient
from nvb_statements.broker import Broker, Delivery
from nvb_statements.config import Settings, get_settings
from nvb_statements.dataset import DatasetIncomplete, DatasetTooLarge, fetch_verified
from nvb_statements.db import as_utc, build_engine, build_sessionmaker
from nvb_statements.logging_setup import configure_logging, request_id_var
from nvb_statements.models import TERMINAL, StatementRequest, utcnow
from nvb_statements.render import render_statement
from nvb_statements.service import record_event
from nvb_statements.storage import ObjectStore, StorageError, sha256_hex

log = logging.getLogger("statements.worker")
TASKS = Counter("statements_worker_tasks_total", "Worker task outcomes", ["outcome"])
DURATION = Histogram("statements_worker_task_seconds", "Task duration", buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300))
LEASE_RENEWALS = Counter("statements_worker_lease_renewals_total", "Lease heartbeats", ["outcome"])

NON_RETRYABLE_ADAPTER = {"not_found": "authorization_revoked", "invalid_subject": "invalid_request",
                         "invalid_period": "invalid_request", "period_too_long": "invalid_request",
                         "invalid_cursor": "dataset_error", "core_integrity_error": "core_integrity_error",
                         "too_large": "too_large",
                         "insufficient_scope": "worker_misconfigured", "invalid_client": "worker_misconfigured"}


class Crash(Exception):
    """Test hook: simulates the process dying at a named point."""


def result_key(request_id: str, dataset_id: str) -> str:
    return f"statements/{request_id}/{dataset_id}.pdf"


class LeaseKeeper:
    """Renews one request's lease every ``lease_seconds / 3`` on its own DB session."""

    def __init__(self, sm: sessionmaker[Session], request_id: str, worker_id: str, lease_seconds: int) -> None:
        self.sm, self.request_id, self.worker_id, self.lease_seconds = sm, request_id, worker_id, lease_seconds
        self.lost = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="lease-keeper", daemon=True)

    def __enter__(self) -> LeaseKeeper:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=10)

    def renew(self) -> bool:
        now = utcnow()
        with self.sm() as db:
            done = db.execute(update(StatementRequest)
                              .where(StatementRequest.id == self.request_id, StatementRequest.status == "running",
                                     StatementRequest.lease_owner == self.worker_id)
                              .values(lease_expires_at=now + timedelta(seconds=self.lease_seconds)))
            db.commit()
        return done.rowcount == 1

    def _run(self) -> None:
        interval = max(1.0, self.lease_seconds / 3)
        while not self._stop.wait(interval):
            try:
                if not self.renew():
                    self.lost = True
                    LEASE_RENEWALS.labels("lost").inc()
                    return
                LEASE_RENEWALS.labels("renewed").inc()
            except Exception:  # noqa: BLE001 - a failed heartbeat must not kill the task; expiry is the fallback
                LEASE_RENEWALS.labels("error").inc()
                log.warning("lease heartbeat failed")


class Worker:
    def __init__(self, settings: Settings, sm: sessionmaker[Session], broker: Broker, store: ObjectStore,
                 adapter: AdapterClient, worker_id: str | None = None, crash_at: str | None = None) -> None:
        self.s, self.sm, self.broker, self.store, self.adapter = settings, sm, broker, store, adapter
        self.worker_id = worker_id or settings.worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.crash_at = crash_at
        self.stopping = False
        self.queues = (settings.queue_name, settings.batch_queue_name)

    def _maybe_crash(self, point: str) -> None:
        if self.crash_at == point:
            raise Crash(point)

    # --- loop -------------------------------------------------------------------------

    def _next(self) -> Delivery | None:
        """Interactive queue first; the batch queue only when no customer request is waiting."""
        d = self.broker.get(self.queues[0], timeout=0)
        if d is None:
            d = self.broker.get(self.queues[1], timeout=0)
        if d is None:
            d = self.broker.get(self.queues[0], timeout=1.0)
        return d

    def run(self, *, drain: bool, max_messages: int | None = None, max_runtime_seconds: float | None = None) -> int:
        handled, started = 0, time.monotonic()
        idle_since = started
        while not self.stopping:
            if max_messages and handled >= max_messages:
                log.info("message bound reached; exiting", extra={"handled": handled})
                return 0
            if max_runtime_seconds and time.monotonic() - started >= max_runtime_seconds:
                log.info("runtime bound reached; exiting", extra={"handled": handled})
                return 0
            d = self._next()
            if d is None:
                if drain and time.monotonic() - idle_since >= self.s.idle_exit_seconds:
                    log.info("queues idle; exiting", extra={"handled": handled})
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
        log.info("stopped on request", extra={"handled": handled})
        return 0

    # --- one message -------------------------------------------------------------------

    def handle(self, d: Delivery) -> str:
        body = d.body
        if body.get("type") != "statement.generate" or not isinstance(body.get("request_id"), str):
            log.error("malformed message dead-lettered", extra={"message_id": d.message_id})
            self.broker.reject(d, requeue=False)
            TASKS.labels("dead_lettered").inc()
            return "dead_lettered"
        rid = body["request_id"][:36]
        token = request_id_var.set(f"stmt-{rid}")
        try:
            outcome = self._process(rid)
        finally:
            request_id_var.reset(token)
        self._maybe_crash("before_ack")
        self.broker.ack(d)
        TASKS.labels(outcome).inc()
        return outcome

    def _claim(self, db: Session, rid: str) -> StatementRequest | None:
        now = utcnow()
        claimed = db.execute(
            update(StatementRequest)
            .where(StatementRequest.id == rid,
                   or_(and_(StatementRequest.status == "queued",
                            or_(StatementRequest.not_before.is_(None), StatementRequest.not_before <= now)),
                       and_(StatementRequest.status == "running", StatementRequest.lease_expires_at < now)))
            .values(status="running", attempts=StatementRequest.attempts + 1, lease_owner=self.worker_id,
                    lease_expires_at=now + timedelta(seconds=self.s.lease_seconds), updated_at=now,
                    started_at=now, not_before=None))
        if claimed.rowcount != 1:
            db.rollback()
            return None
        db.commit()
        req = db.get(StatementRequest, rid)
        db.commit()  # end the read transaction: nothing is held open while the task runs
        return req

    def _process(self, rid: str) -> str:
        started = time.perf_counter()
        with self.sm() as db:
            req = self._claim(db, rid)
            if req is None:
                existing = db.get(StatementRequest, rid)
                db.rollback()
                if existing is None:
                    log.warning("unknown request id; acked")
                    return "unknown"
                # terminal, not yet due, or leased by a live worker: nothing to do here.
                return "duplicate" if existing.status not in TERMINAL else "already_" + existing.status
            self._maybe_crash("after_claim")
            if req.attempts > self.s.max_attempts:
                return self._fail(db, req, "attempts_exhausted", "Too many attempts.")
            with LeaseKeeper(self.sm, req.id, self.worker_id, self.s.lease_seconds) as lease:
                try:
                    ds = fetch_verified(self.adapter, account_id=req.account_id, subject=req.owner_subject,
                                        start=as_utc(req.period_start_utc), end=as_utc(req.period_end_utc),
                                        page_size=self.s.entries_page_size,
                                        max_entries=self.s.max_entries_per_statement,
                                        request_id=request_id_var.get(), on_page=self.broker.keepalive)
                    pdf = render_statement(manifest=ds.manifest, entries=ds.entries, period_label=req.period_label,
                                           timezone=req.period_timezone, request_id=req.id)
                    self.broker.keepalive()
                    key = result_key(req.id, ds.manifest["dataset_id"])
                    digest = sha256_hex(pdf)
                    self.store.put(key, pdf, digest)
                    self._maybe_crash("after_upload")
                except AdapterCallError as exc:
                    if not exc.retryable and exc.code in NON_RETRYABLE_ADAPTER:
                        return self._fail(db, req, NON_RETRYABLE_ADAPTER[exc.code],
                                          "The statement could not be produced.")
                    if not exc.retryable and exc.status_code < 500 and exc.code != "period_not_settled":
                        return self._fail(db, req, "invalid_request", "The statement could not be produced.")
                    return self._retry(db, req, exc.code)
                except DatasetTooLarge:
                    return self._fail(db, req, "too_large", "The statement is too large to generate.")
                except DatasetIncomplete:
                    return self._retry(db, req, "dataset_incomplete")
                except StorageError:
                    return self._retry(db, req, "storage_unavailable")
                if lease.lost:
                    log.warning("lease lost while processing; another worker owns the request")
                    return "lease_lost"

            now = utcnow()
            done = db.execute(update(StatementRequest)
                              .where(StatementRequest.id == req.id, StatementRequest.status == "running",
                                     StatementRequest.lease_owner == self.worker_id)
                              .values(status="completed", result_key=key, result_sha256=digest,
                                      result_size=len(pdf), dataset_id=ds.manifest["dataset_id"],
                                      entry_count=ds.manifest["entry_count"], completed_at=now, updated_at=now,
                                      lease_owner=None, lease_expires_at=None, error_code=None,
                                      error_message=None))
            if done.rowcount != 1:
                db.rollback()
                log.warning("lease lost before completion; another worker owns the request")
                return "lease_lost"
            record_event(db, req.id, "completed", f"worker:{self.worker_id}", dataset_id=ds.manifest["dataset_id"],
                         entry_count=ds.manifest["entry_count"], size=len(pdf))
            db.commit()
            self._maybe_crash("after_commit")
            DURATION.observe(time.perf_counter() - started)
            log.info("statement completed", extra={"entries": ds.manifest["entry_count"], "bytes": len(pdf)})
            return "completed"

    def _fail(self, db: Session, req: StatementRequest, code: str, message: str) -> str:
        now = utcnow()
        db.execute(update(StatementRequest).where(StatementRequest.id == req.id,
                                                  StatementRequest.lease_owner == self.worker_id)
                   .values(status="failed", error_code=code, error_message=message[:200], completed_at=now,
                           updated_at=now, lease_owner=None, lease_expires_at=None))
        record_event(db, req.id, "failed", f"worker:{self.worker_id}", code=code, attempts=req.attempts)
        db.commit()
        log.warning("statement failed", extra={"code": code, "attempts": req.attempts})
        return "failed"

    def _retry(self, db: Session, req: StatementRequest, code: str) -> str:
        if req.attempts >= self.s.max_attempts:
            return self._fail(db, req, "attempts_exhausted", "The statement could not be produced; try again later.")
        delay = min(self.s.retry_max_seconds, self.s.retry_base_seconds * (2 ** (req.attempts - 1)))
        now = utcnow()
        db.execute(update(StatementRequest).where(StatementRequest.id == req.id,
                                                  StatementRequest.lease_owner == self.worker_id)
                   .values(status="queued", not_before=now + timedelta(seconds=delay), error_code=code,
                           updated_at=now, lease_owner=None, lease_expires_at=None))
        record_event(db, req.id, "retry_scheduled", f"worker:{self.worker_id}", code=code, delay_seconds=delay,
                     attempts=req.attempts)
        db.commit()
        log.info("retry scheduled", extra={"code": code, "delay_seconds": delay})
        return "retry_scheduled"


def main(argv: list[str] | None = None) -> int:
    from nvb_statements.broker import BrokerUnavailable, RabbitBroker
    from nvb_statements.storage import build_store

    p = argparse.ArgumentParser(prog="statements-worker")
    p.add_argument("--drain", action="store_true", help="finite run: exit once the queues are idle (Job mode)")
    p.add_argument("--max-messages", type=int, default=None,
                   help="override STATEMENTS_WORKER_MAX_MESSAGES (0 = unbounded)")
    p.add_argument("--max-runtime-seconds", type=int, default=None,
                   help="override STATEMENTS_WORKER_MAX_RUNTIME_SECONDS (0 = unbounded)")
    args = p.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level, "statements-worker", f"{socket.gethostname()}:{os.getpid()}")
    problems = settings.validate_runtime("worker")
    if problems:
        log.error("invalid configuration", extra={"problems": problems})
        return 78
    try:
        broker = RabbitBroker(settings, verify=(settings.queue_name, settings.batch_queue_name))
    except BrokerUnavailable as exc:
        log.error("broker unavailable or queues missing (the relay declares them)", extra={"error": str(exc)})
        return 75
    worker = Worker(settings, build_sessionmaker(build_engine(settings, "nvb-statements-worker")), broker,
                    build_store(settings), AdapterClient(settings))

    def stop(*_):
        log.info("termination requested; finishing current message")
        worker.stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    max_messages = settings.worker_max_messages if args.max_messages is None else args.max_messages
    max_runtime = settings.worker_max_runtime_seconds if args.max_runtime_seconds is None \
        else args.max_runtime_seconds
    try:
        return worker.run(drain=args.drain, max_messages=max_messages or None,
                          max_runtime_seconds=max_runtime or None)
    finally:
        worker.broker.close()
        worker.adapter.close()


if __name__ == "__main__":
    sys.exit(main())
