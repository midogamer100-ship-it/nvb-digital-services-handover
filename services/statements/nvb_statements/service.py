"""Statement request operations shared by the API, batch trigger and worker.

Creating a request writes the request row, an audit event and an outbox message in ONE
database transaction. The relay publishes outbox rows later, so a crash between "DB commit"
and "publish" loses nothing.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nvb_statements.models import OutboxMessage, StatementEvent, StatementRequest, utcnow
from nvb_statements.period import MonthPeriod

DATASET_VERSION = "statement-v2"


class ServiceError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code, self.code, self.message = status_code, code, message


@dataclass
class CreateResult:
    request: StatementRequest
    replayed: bool


def fingerprint(account_id: int, period: MonthPeriod, purpose: str) -> str:
    raw = json.dumps({"a": account_id, "p": period.label, "tz": period.timezone, "u": purpose}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def record_event(db: Session, request_id: str, event_type: str, actor: str, **details) -> None:
    db.add(StatementEvent(request_id=request_id, event_type=event_type, actor=actor, details=details,
                          created_at=utcnow()))


def enqueue(db: Session, request_id: str, queue: str, attempt_hint: int = 0) -> None:
    db.add(OutboxMessage(message_id=str(uuid.uuid4()), queue=queue,
                         payload={"type": "statement.generate", "request_id": request_id, "attempt": attempt_hint},
                         created_at=utcnow(), publish_attempts=0))


def build_request(*, owner_subject: str, requested_by: str, account_id: int, account_masked: str,
                  period: MonthPeriod, idempotency_key: str, queue: str, purpose: str,
                  batch_id: str | None) -> StatementRequest:
    now = utcnow()
    return StatementRequest(
        id=str(uuid.uuid4()), owner_subject=owner_subject, requested_by_subject=requested_by, purpose=purpose,
        batch_id=batch_id, account_id=account_id, account_masked=account_masked[:16],
        data_scope="statement:single_account", period_label=period.label, period_timezone=period.timezone,
        period_start_utc=period.start_utc, period_end_utc=period.end_utc, dataset_version=DATASET_VERSION,
        status="queued", attempts=0, idempotency_key=idempotency_key,
        request_fingerprint=fingerprint(account_id, period, purpose), queue=queue, created_at=now, updated_at=now)


def create_request(db: Session, *, owner_subject: str, requested_by: str, account: dict, period: MonthPeriod,
                   idempotency_key: str, queue: str, purpose: str = "customer_request",
                   batch_id: str | None = None) -> CreateResult:
    fp = fingerprint(account["account_id"], period, purpose)
    existing = db.scalar(select(StatementRequest).where(StatementRequest.owner_subject == owner_subject,
                                                        StatementRequest.idempotency_key == idempotency_key))
    if existing is not None:
        if existing.request_fingerprint != fp:
            raise ServiceError(409, "idempotency_conflict",
                               "This Idempotency-Key was already used for a different request.")
        return CreateResult(existing, replayed=True)

    req = build_request(owner_subject=owner_subject, requested_by=requested_by, account_id=account["account_id"],
                        account_masked=account["masked_number"], period=period, idempotency_key=idempotency_key,
                        queue=queue, purpose=purpose, batch_id=batch_id)
    db.add(req)
    record_event(db, req.id, "requested", requested_by, account_id=req.account_id, period=period.label,
                 purpose=purpose)
    enqueue(db, req.id, queue)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()  # concurrent duplicate with the same key won the unique constraint
        existing = db.scalar(select(StatementRequest).where(StatementRequest.owner_subject == owner_subject,
                                                            StatementRequest.idempotency_key == idempotency_key))
        if existing is None:
            raise
        if existing.request_fingerprint != fp:
            raise ServiceError(409, "idempotency_conflict",
                               "This Idempotency-Key was already used for a different request.") from None
        return CreateResult(existing, replayed=True)
    return CreateResult(req, replayed=False)
