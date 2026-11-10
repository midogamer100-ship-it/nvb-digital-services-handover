"""Batch orchestration for statements (same code path for manual and scheduled runs).

``create_batch`` + ``advance_batch`` are called by

* the staff API (``POST /x/statements/v1/batches``, role ``batch_operator``), and
* the scheduled trigger (``python -m nvb_statements.batch create --trigger schedule ...``,
  run by a Kubernetes CronJob),

so both have identical business semantics. The work is split in resumable, idempotent steps:

1. **freeze** - page the core's authorised account discovery through the Core Adapter and
   store the members in ``batch_items`` (the frozen manifest). The cursor is persisted with
   every page, so a crash resumes where it stopped. Nothing is enqueued during this phase.
2. **release** - create one ``statement_requests`` row (+ audit event + outbox message on the
   BATCH queue) per member, in chunks. ``batch_items.request_id`` is set in the same
   transaction, so a member is never enqueued twice.
3. **run** - workers process the members like any other request (leases, bounded retries,
   idempotent output). Progress is counted from the request rows.

A retry of the same create call (same Idempotency-Key) resumes an unfinished batch instead of
creating a second one. ``retry_failed`` re-queues failed members for a bounded number of rounds.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from nvb_platform.adapter_client import AdapterCallError
from nvb_statements.config import Settings, get_settings
from nvb_statements.models import (
    BATCH_TERMINAL,
    TERMINAL,
    BatchItem,
    StatementBatch,
    StatementRequest,
    utcnow,
)
from nvb_statements.period import PeriodError, month_period, validate_requestable
from nvb_statements.service import ServiceError, build_request, enqueue, record_event

log = logging.getLogger("statements.batch")


def previous_month_label(now: datetime, tz_name: str) -> str:
    local = now.astimezone(ZoneInfo(tz_name))
    year, month = (local.year - 1, 12) if local.month == 1 else (local.year, local.month - 1)
    return f"{year:04d}-{month:02d}"


# --- create ------------------------------------------------------------------------------


def create_batch(db: Session, settings: Settings, *, period_label: str, created_by: str, trigger: str,
                 idempotency_key: str, include_load_test: bool = False) -> tuple[StatementBatch, bool]:
    """Insert the batch row (status ``freezing``) or return the existing one for this key."""
    try:
        period = month_period(period_label, settings.statement_timezone)
        validate_requestable(period, now=utcnow(), settle_seconds=settings.settle_seconds,
                             max_months_back=settings.max_months_back)
    except PeriodError as exc:
        raise ServiceError(422, exc.code, str(exc)) from None
    existing = db.scalar(select(StatementBatch).where(StatementBatch.idempotency_key == idempotency_key))
    if existing is None:
        now = utcnow()
        batch = StatementBatch(
            id=str(uuid.uuid4()), kind="monthly_statements", period_label=period.label,
            period_timezone=period.timezone, period_start_utc=period.start_utc, period_end_utc=period.end_utc,
            status="freezing", trigger_kind=trigger, created_by=created_by[:80], idempotency_key=idempotency_key,
            include_load_test=include_load_test, discovery_cursor=0, total_items=0, released_items=0,
            retry_rounds=0, created_at=now, updated_at=now)
        db.add(batch)
        try:
            db.commit()
            log.info("batch created", extra={"batch_id": batch.id, "period": period.label, "trigger": trigger})
            return batch, True
        except IntegrityError:
            db.rollback()
            existing = db.scalar(select(StatementBatch).where(StatementBatch.idempotency_key == idempotency_key))
            if existing is None:
                raise
    if existing.period_label != period.label or existing.include_load_test != include_load_test:
        raise ServiceError(409, "idempotency_conflict", "This Idempotency-Key was already used for a different batch.")
    return existing, False


# --- freeze + release (resumable) ----------------------------------------------------------


def _locked(db: Session, batch_id: str) -> StatementBatch:
    return db.scalar(select(StatementBatch).where(StatementBatch.id == batch_id).with_for_update())


def _freeze_page(sm: sessionmaker[Session], adapter, settings: Settings, batch_id: str) -> bool:
    """Store one discovery page. Returns False when freezing is finished (or the batch left the phase)."""
    with sm() as db:
        row = db.execute(select(StatementBatch.discovery_cursor, StatementBatch.include_load_test)
                         .where(StatementBatch.id == batch_id, StatementBatch.status == "freezing")).first()
    if row is None:
        return False
    cursor = row[0]
    # The adapter call happens OUTSIDE any database transaction.
    page = adapter.discovery(cursor, settings.batch_discovery_page_size, f"batch-{batch_id}", row[1])
    with sm() as db:
        batch = _locked(db, batch_id)
        if batch.status != "freezing" or batch.discovery_cursor != cursor:
            db.rollback()  # another caller advanced this batch meanwhile; re-read on the next loop
            return batch.status == "freezing"
        known = set(db.scalars(select(BatchItem.account_id).where(
            BatchItem.batch_id == batch_id, BatchItem.account_id.in_([a["account_id"] for a in page["items"]] or [0]))))
        added = 0
        for a in page["items"]:
            if a["account_id"] in known:
                continue
            db.add(BatchItem(batch_id=batch_id, owner_subject=a["subject"], account_id=a["account_id"],
                             account_masked=a["masked_number"][:16]))
            added += 1
        batch.total_items += added
        batch.updated_at = utcnow()
        if batch.total_items > settings.batch_max_accounts:
            batch.status, batch.error_code, batch.finished_at = "failed", "too_many_accounts", utcnow()
            db.commit()
            log.error("batch exceeds the configured account cap", extra={"batch_id": batch_id})
            return False
        if page["has_more"] and page["next_after_account_id"] is not None:
            batch.discovery_cursor = page["next_after_account_id"]
            db.commit()
            return True
        batch.status, batch.frozen_at = "releasing", utcnow()
        db.commit()
        log.info("batch manifest frozen", extra={"batch_id": batch_id, "members": batch.total_items})
        return False


def _release_chunk(sm: sessionmaker[Session], settings: Settings, batch_id: str) -> bool:
    """Create requests + outbox rows for one chunk of unreleased members. False when finished."""
    with sm() as db:
        batch = _locked(db, batch_id)
        if batch.status != "releasing":
            db.rollback()
            return False
        items = db.scalars(select(BatchItem).where(BatchItem.batch_id == batch_id, BatchItem.request_id.is_(None))
                           .order_by(BatchItem.id).limit(settings.batch_release_chunk)).all()
        if not items:
            batch.status, batch.updated_at = "running", utcnow()
            if batch.total_items == 0:
                batch.status, batch.finished_at = "completed", utcnow()
            db.commit()
            log.info("batch released", extra={"batch_id": batch_id, "members": batch.total_items})
            return False
        period = month_period(batch.period_label, batch.period_timezone)
        for item in items:
            req = build_request(
                owner_subject=item.owner_subject, requested_by=batch.created_by, account_id=item.account_id,
                account_masked=item.account_masked, period=period,
                idempotency_key=f"batch-{batch_id}-{item.account_id}"[:64], queue=settings.batch_queue_name,
                purpose="batch", batch_id=batch_id)
            db.add(req)
            record_event(db, req.id, "requested", batch.created_by, account_id=req.account_id,
                         period=period.label, purpose="batch", batch_id=batch_id)
            enqueue(db, req.id, settings.batch_queue_name)
            item.request_id = req.id
        batch.released_items += len(items)
        batch.updated_at = utcnow()
        db.commit()
        return True


def advance_batch(sm: sessionmaker[Session], adapter, settings: Settings, batch_id: str) -> StatementBatch:
    """Run freeze and release to completion (resumable; safe to call again after any failure)."""
    while _freeze_page(sm, adapter, settings, batch_id):
        pass
    while _release_chunk(sm, settings, batch_id):
        pass
    with sm() as db:
        return db.get(StatementBatch, batch_id)


# --- progress / finalize ---------------------------------------------------------------------


def progress(db: Session, batch: StatementBatch) -> dict:
    counts = dict(db.execute(select(StatementRequest.status, func.count())
                             .where(StatementRequest.batch_id == batch.id)
                             .group_by(StatementRequest.status)).all())
    out = {s: int(counts.get(s, 0)) for s in ("queued", "running", "completed", "failed", "cancelled")}
    out["total"] = batch.total_items
    out["pending_release"] = max(0, batch.total_items - batch.released_items)
    return out


def finalize_if_done(db: Session, batch: StatementBatch) -> StatementBatch:
    """running -> completed / completed_with_failures once every member is terminal. Caller owns the session."""
    if batch.status != "running":
        return batch
    p = progress(db, batch)
    if p["pending_release"] or p["queued"] or p["running"]:
        return batch
    status = "completed_with_failures" if p["failed"] or p["cancelled"] else "completed"
    changed = db.execute(update(StatementBatch).where(StatementBatch.id == batch.id,
                                                      StatementBatch.status == "running")
                         .values(status=status, finished_at=utcnow(), updated_at=utcnow()))
    db.commit()
    if changed.rowcount == 1:
        log.info("batch finished", extra={"batch_id": batch.id, "status": status, **p})
    db.refresh(batch)
    return batch


def finalize_running(sm: sessionmaker[Session], limit: int = 50) -> int:
    """Called by the relay loop so batches finish even when nobody polls the API."""
    n = 0
    with sm() as db:
        for batch in db.scalars(select(StatementBatch).where(StatementBatch.status == "running").limit(limit)).all():
            if finalize_if_done(db, batch).status != "running":
                n += 1
    return n


def batch_out(db: Session, batch: StatementBatch) -> dict:
    from nvb_statements.db import as_utc

    def ts(v):
        return as_utc(v).isoformat() if v else None

    return {"id": batch.id, "kind": batch.kind, "status": batch.status, "trigger": batch.trigger_kind,
            "created_by": batch.created_by,
            "period": {"label": batch.period_label, "timezone": batch.period_timezone,
                       "start_utc": ts(batch.period_start_utc), "end_utc": ts(batch.period_end_utc)},
            "progress": progress(db, batch), "retry_rounds": batch.retry_rounds, "error_code": batch.error_code,
            "created_at": ts(batch.created_at), "frozen_at": ts(batch.frozen_at), "finished_at": ts(batch.finished_at)}


# --- operator actions ------------------------------------------------------------------------


def retry_failed(db: Session, settings: Settings, batch_id: str, actor: str) -> int:
    """Re-queue the FAILED members of a batch. Bounded by ``batch_max_retry_rounds``."""
    batch = _locked(db, batch_id)
    if batch is None:
        raise ServiceError(404, "not_found", "Batch not found.")
    if batch.status not in ("running", "completed_with_failures"):
        db.rollback()
        raise ServiceError(409, "not_retryable", "Only running or finished-with-failures batches can be retried.")
    if batch.retry_rounds >= settings.batch_max_retry_rounds:
        db.rollback()
        raise ServiceError(409, "retry_limit_reached", "The retry limit for this batch has been reached.")
    failed = db.scalars(select(StatementRequest).where(StatementRequest.batch_id == batch_id,
                                                       StatementRequest.status == "failed").with_for_update()).all()
    now = utcnow()
    for r in failed:
        r.status, r.attempts, r.not_before, r.error_code, r.error_message = "queued", 0, None, None, None
        r.completed_at, r.updated_at, r.lease_owner, r.lease_expires_at = None, now, None, None
        enqueue(db, r.id, r.queue)
        record_event(db, r.id, "batch_retry", actor, batch_id=batch_id, round=batch.retry_rounds + 1)
    if failed:
        batch.retry_rounds += 1
        batch.status, batch.finished_at, batch.updated_at = "running", None, now
    db.commit()
    log.info("batch retry", extra={"batch_id": batch_id, "requeued": len(failed), "round": batch.retry_rounds})
    return len(failed)


def cancel_batch(db: Session, batch_id: str, actor: str) -> int:
    """Stop a batch: unreleased members are never released; queued members are cancelled.
    Members already running finish normally. Completed results are kept."""
    batch = _locked(db, batch_id)
    if batch is None:
        raise ServiceError(404, "not_found", "Batch not found.")
    if batch.status in BATCH_TERMINAL:
        db.rollback()
        raise ServiceError(409, "not_cancellable", "The batch has already finished.")
    now = utcnow()
    queued = db.scalars(select(StatementRequest).where(StatementRequest.batch_id == batch_id,
                                                       StatementRequest.status == "queued").with_for_update()).all()
    for r in queued:
        r.status, r.completed_at, r.updated_at = "cancelled", now, now
        record_event(db, r.id, "cancelled", actor, batch_id=batch_id)
    batch.status, batch.finished_at, batch.updated_at = "cancelled", now, now
    db.commit()
    log.info("batch cancelled", extra={"batch_id": batch_id, "cancelled_members": len(queued)})
    return len(queued)


def failed_items(db: Session, batch_id: str, limit: int = 100) -> list[dict]:
    rows = db.scalars(select(StatementRequest).where(StatementRequest.batch_id == batch_id,
                                                     StatementRequest.status == "failed")
                      .order_by(StatementRequest.updated_at.desc()).limit(limit)).all()
    return [{"request_id": r.id, "account_masked": r.account_masked, "error_code": r.error_code,
             "attempts": r.attempts} for r in rows]


assert TERMINAL  # re-exported name kept for readers of this module


# --- CLI (scheduled trigger and operator command) ----------------------------------------------


def main(argv: list[str] | None = None) -> int:
    from nvb_platform.adapter_client import AdapterClient
    from nvb_platform.logging_setup import configure_logging
    from nvb_statements.db import build_engine, build_sessionmaker

    p = argparse.ArgumentParser(prog="statements-batch", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("create", help="create (or resume) a batch and release its members")
    c.add_argument("--period", required=True, help="YYYY-MM or 'previous-month' (in STATEMENTS_STATEMENT_TIMEZONE)")
    c.add_argument("--trigger", choices=["manual", "schedule"], default="schedule")
    c.add_argument("--actor", default="system:schedule")
    c.add_argument("--idempotency-key", help="default: <trigger>-<period> (one scheduled batch per period)")
    c.add_argument("--include-load-test", action="store_true")
    r = sub.add_parser("resume", help="resume an unfinished batch")
    r.add_argument("batch_id")
    args = p.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, "statements-batch", f"{socket.gethostname()}:{os.getpid()}")
    problems = settings.validate_runtime("batch")
    if problems:
        log.error("invalid configuration", extra={"problems": problems})
        return 78
    sm = build_sessionmaker(build_engine(settings, "nvb-statements-batch"))
    adapter = AdapterClient(settings)
    try:
        if args.command == "create":
            label = previous_month_label(utcnow(), settings.statement_timezone) \
                if args.period == "previous-month" else args.period
            key = args.idempotency_key or f"{args.trigger}-{label}"
            with sm() as db:
                batch, _created = create_batch(db, settings, period_label=label, created_by=args.actor,
                                               trigger=args.trigger, idempotency_key=key,
                                               include_load_test=args.include_load_test)
            batch_id = batch.id
        else:
            batch_id = args.batch_id
        batch = advance_batch(sm, adapter, settings, batch_id)
        if batch is None:
            log.error("batch not found")
            return 1
        with sm() as db:
            print(json.dumps(batch_out(db, db.get(StatementBatch, batch_id))))
        return 0 if batch.status != "failed" else 1
    except ServiceError as exc:
        log.error("batch rejected", extra={"code": exc.code})
        return 2
    except AdapterCallError as exc:
        # Resumable: the CronJob retry (same idempotency key) continues from the stored cursor.
        log.error("core adapter unavailable; batch can be resumed", extra={"code": exc.code})
        return 75
    finally:
        adapter.close()


if __name__ == "__main__":
    sys.exit(main())
