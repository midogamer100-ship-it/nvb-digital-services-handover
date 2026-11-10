r"""Statements extension schema. No foreign keys into core tables (core ids are plain values).

State machine (statement_requests.status)::

    queued --claim--> running --success--> completed
      ^                 |  \--non-retryable / attempts exhausted--> failed
      |                 |
      +--retryable error (not_before = backoff) / lease expired (reconciler)
    queued --customer cancel--> cancelled          (running requests cannot be cancelled)
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from nvb_platform.roles import RoleAssignmentMixin, RoleEventMixin

STATUSES = ("queued", "running", "completed", "failed", "cancelled")
TERMINAL = {"completed", "failed", "cancelled"}
BATCH_STATUSES = ("freezing", "releasing", "running", "completed", "completed_with_failures", "cancelled",
                  "failed")
BATCH_TERMINAL = {"completed", "completed_with_failures", "cancelled", "failed"}
# Extension roles known to THIS service. Core roles (customer/admin/demo) grant none of them.
KNOWN_ROLES = frozenset({"batch_operator", "extension_admin"})
BigIntPK = BigInteger().with_variant(Integer, "sqlite")


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class StatementRequest(Base):
    __tablename__ = "statement_requests"
    __table_args__ = (
        UniqueConstraint("owner_subject", "idempotency_key", name="uq_statement_idempotency"),
        CheckConstraint("status IN ('queued','running','completed','failed','cancelled')", name="ck_statement_status"),
        CheckConstraint("purpose IN ('customer_request','batch')", name="ck_statement_purpose"),
        Index("ix_statement_owner_created", "owner_subject", "created_at"),
        Index("ix_statement_status_not_before", "status", "not_before"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_subject: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_by_subject: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose: Mapped[str] = mapped_column(String(20), nullable=False)
    batch_id: Mapped[str | None] = mapped_column(String(36), index=True)
    account_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    account_masked: Mapped[str] = mapped_column(String(16), nullable=False)
    data_scope: Mapped[str] = mapped_column(String(40), nullable=False, default="statement:single_account")
    period_label: Mapped[str] = mapped_column(String(7), nullable=False)  # YYYY-MM
    period_timezone: Mapped[str] = mapped_column(String(40), nullable=False)
    period_start_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    dataset_version: Mapped[str] = mapped_column(String(20), nullable=False, default="statement-v2")
    # Broker queue this request is (re-)published to: interactive or batch.
    queue: Mapped[str] = mapped_column(String(80), nullable=False, default="nvb.statements.generate")
    dataset_id: Mapped[str | None] = mapped_column(String(64))
    entry_count: Mapped[int | None] = mapped_column(Integer)

    status: Mapped[str] = mapped_column(String(12), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(80))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    result_key: Mapped[str | None] = mapped_column(String(200))
    result_sha256: Mapped[str | None] = mapped_column(String(64))
    result_size: Mapped[int | None] = mapped_column(BigInteger)
    error_code: Mapped[str | None] = mapped_column(String(40))
    error_message: Mapped[str | None] = mapped_column(String(200))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StatementEvent(Base):
    """Append-only audit trail. The application role gets INSERT/SELECT only (see migration)."""

    __tablename__ = "statement_events"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    actor: Mapped[str] = mapped_column(String(80), nullable=False)
    details: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class OutboxMessage(Base):
    """Transactional outbox for broker publishing (unrelated to the core's demo mailbox)."""

    __tablename__ = "outbox_messages"
    __table_args__ = (Index("ix_outbox_unpublished", "published_at", "id"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    message_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    queue: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    publish_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(200))


class StatementBatch(Base):
    r"""One batch run (e.g. monthly statements for every eligible account).

    ::

        freezing --discovery complete--> releasing --all members enqueued--> running
        running --all members terminal--> completed | completed_with_failures
        any non-terminal --operator cancel--> cancelled ;  freezing --over the cap--> failed

    ``freezing`` builds the frozen member manifest (``batch_items``) from the authorised
    discovery of the core, resumably (``discovery_cursor``). Nothing is processed before the
    manifest is complete. Manual (API) and scheduled (CronJob) creation call the same function.
    """

    __tablename__ = "statement_batches"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_batch_idempotency"),
        CheckConstraint("status IN ('freezing','releasing','running','completed','completed_with_failures',"
                        "'cancelled','failed')", name="ck_batch_status"),
        CheckConstraint("trigger_kind IN ('manual','schedule')", name="ck_batch_trigger"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="monthly_statements")
    period_label: Mapped[str] = mapped_column(String(7), nullable=False)
    period_timezone: Mapped[str] = mapped_column(String(40), nullable=False)
    period_start_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="freezing")
    trigger_kind: Mapped[str] = mapped_column(String(10), nullable=False)
    created_by: Mapped[str] = mapped_column(String(80), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    include_load_test: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    discovery_cursor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    released_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_rounds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BatchItem(Base):
    """Frozen manifest member: one (subject, account) captured at freeze time."""

    __tablename__ = "batch_items"
    __table_args__ = (UniqueConstraint("batch_id", "account_id", name="uq_batch_item_account"),
                      Index("ix_batch_item_unreleased", "batch_id", "request_id"))

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    owner_subject: Mapped[str] = mapped_column(String(64), nullable=False)
    account_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    account_masked: Mapped[str] = mapped_column(String(16), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(36))


class RoleAssignment(RoleAssignmentMixin, Base):
    pass


class RoleEvent(RoleEventMixin, Base):
    pass
