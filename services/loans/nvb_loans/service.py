"""Loan application workflow rules (shared by the API, the batch command and tests).

Every transition is one conditional UPDATE ``WHERE id = :id AND version = :expected AND
status = :from`` that bumps ``version``. Two staff members acting on the same version cannot
both succeed: the loser gets 409 ``version_conflict``. Each transition writes an append-only
``loan_events`` row with the actor's stable subject in the same transaction.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nvb_loans.models import (
    OPEN_FOR_REVIEW,
    LoanApplication,
    LoanBatch,
    LoanEvent,
    OutboxMessage,
    SummaryTask,
    utcnow,
)


class ServiceError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code, self.code, self.message = status_code, code, message


def event(db: Session, app_id: str, event_type: str, actor: str, from_status: str | None = None,
          to_status: str | None = None, **details) -> None:
    db.add(LoanEvent(application_id=app_id, event_type=event_type, actor=actor[:80], from_status=from_status,
                     to_status=to_status, details=details, created_at=utcnow()))


def enqueue_summary(db: Session, app_id: str, queue: str, batch_id: str | None = None) -> SummaryTask:
    now = utcnow()
    task = SummaryTask(id=str(uuid.uuid4()), application_id=app_id, batch_id=batch_id, status="queued", attempts=0,
                       created_at=now, updated_at=now)
    db.add(task)
    publish_task(db, task.id, queue)
    return task


def publish_task(db: Session, task_id: str, queue: str) -> None:
    db.add(OutboxMessage(message_id=str(uuid.uuid4()), queue=queue,
                         payload={"type": "loan.summarize", "task_id": task_id}, created_at=utcnow(),
                         publish_attempts=0))


def transition(db: Session, app: LoanApplication, expected_version: int, from_status: tuple[str, ...],
               values: dict) -> None:
    """Apply a guarded transition. Raises 409 on a stale version or an invalid source state."""
    if app.status not in from_status:
        raise ServiceError(409, "invalid_state", f"Not allowed while the application is '{app.status}'.")
    done = db.execute(update(LoanApplication)
                      .where(LoanApplication.id == app.id, LoanApplication.version == expected_version,
                             LoanApplication.status.in_(from_status))
                      .values(version=LoanApplication.version + 1, updated_at=utcnow(), **values))
    if done.rowcount != 1:
        db.rollback()
        raise ServiceError(409, "version_conflict",
                           "The application was changed by someone else. Reload and try again.")


# --- batches (summary refresh for every application open for review) ---------------------------


def create_summary_batch(db: Session, *, queue: str, created_by: str, trigger: str, idempotency_key: str,
                         max_applications: int) -> tuple[LoanBatch, bool]:
    """Create the batch and its frozen member list atomically. Idempotent per key."""
    existing = db.scalar(select(LoanBatch).where(LoanBatch.idempotency_key == idempotency_key))
    if existing is not None:
        return existing, False
    ids = db.scalars(select(LoanApplication.id).where(LoanApplication.status.in_(OPEN_FOR_REVIEW))
                     .order_by(LoanApplication.created_at, LoanApplication.id).limit(max_applications + 1)).all()
    if len(ids) > max_applications:
        raise ServiceError(422, "too_many_applications", "More applications than the configured batch limit.")
    now = utcnow()
    batch = LoanBatch(id=str(uuid.uuid4()), status="running" if ids else "completed", trigger_kind=trigger,
                      created_by=created_by[:80], idempotency_key=idempotency_key, total_items=len(ids),
                      retry_rounds=0, created_at=now, finished_at=None if ids else now)
    db.add(batch)
    for app_id in ids:
        enqueue_summary(db, app_id, queue, batch.id)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(LoanBatch).where(LoanBatch.idempotency_key == idempotency_key))
        if existing is None:
            raise
        return existing, False
    return batch, True


def batch_progress(db: Session, batch: LoanBatch) -> dict:
    counts = dict(db.execute(select(SummaryTask.status, func.count()).where(SummaryTask.batch_id == batch.id)
                             .group_by(SummaryTask.status)).all())
    return {**{s: int(counts.get(s, 0)) for s in ("queued", "running", "completed", "failed")},
            "total": batch.total_items}


def finalize_batch(db: Session, batch: LoanBatch) -> LoanBatch:
    if batch.status != "running":
        return batch
    p = batch_progress(db, batch)
    if p["queued"] or p["running"]:
        return batch
    status = "completed_with_failures" if p["failed"] else "completed"
    db.execute(update(LoanBatch).where(LoanBatch.id == batch.id, LoanBatch.status == "running")
               .values(status=status, finished_at=utcnow()))
    db.commit()
    db.refresh(batch)
    return batch


def retry_failed(db: Session, batch_id: str, queue: str, max_rounds: int) -> int:
    batch = db.scalar(select(LoanBatch).where(LoanBatch.id == batch_id).with_for_update())
    if batch is None:
        raise ServiceError(404, "not_found", "Batch not found.")
    if batch.retry_rounds >= max_rounds:
        db.rollback()
        raise ServiceError(409, "retry_limit_reached", "The retry limit for this batch has been reached.")
    failed = db.scalars(select(SummaryTask).where(SummaryTask.batch_id == batch_id, SummaryTask.status == "failed")
                        .with_for_update()).all()
    if not failed:
        db.rollback()
        raise ServiceError(409, "nothing_to_retry", "The batch has no failed members.")
    now = utcnow()
    for t in failed:
        t.status, t.attempts, t.not_before, t.error_code, t.completed_at, t.updated_at = "queued", 0, None, None, None, now
        publish_task(db, t.id, queue)
    batch.retry_rounds += 1
    batch.status, batch.finished_at = "running", None
    db.commit()
    return len(failed)
