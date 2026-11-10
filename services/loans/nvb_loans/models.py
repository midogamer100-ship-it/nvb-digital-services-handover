r"""Loans extension schema (own database; no foreign keys into core tables).

Application workflow (``loan_applications.status``)::

    draft --customer submit--> submitted --assign reviewer--> under_review
    under_review --approver decision--> approved | rejected

"approved" is a WORKFLOW decision only. Nothing here posts money, calls a core deposit or
creates a financial loan record. Money values are integer minor units.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, BigInteger, CheckConstraint, DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from nvb_platform.roles import RoleAssignmentMixin, RoleEventMixin

STATUSES = ("draft", "submitted", "under_review", "approved", "rejected")
OPEN_FOR_REVIEW = ("submitted", "under_review")
KNOWN_ROLES = frozenset({"reviewer", "approver", "batch_operator", "extension_admin"})
TASK_TERMINAL = {"completed", "failed"}
BigIntPK = BigInteger().with_variant(Integer, "sqlite")
TS = DateTime(timezone=True)


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class LoanApplication(Base):
    __tablename__ = "loan_applications"
    __table_args__ = (
        CheckConstraint("status IN ('draft','submitted','under_review','approved','rejected')", name="ck_loan_status"),
        CheckConstraint("amount_minor > 0 AND term_months > 0 AND monthly_income_minor >= 0", name="ck_loan_values"),
        Index("ix_loan_owner", "owner_subject", "created_at"),
        Index("ix_loan_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_subject: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_label: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    account_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    account_masked: Mapped[str] = mapped_column(String(16), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    term_months: Mapped[int] = mapped_column(Integer, nullable=False)
    monthly_income_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    purpose: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)  # optimistic concurrency
    reviewer_subject: Mapped[str | None] = mapped_column(String(64), index=True)
    assigned_by: Mapped[str | None] = mapped_column(String(64))
    recommendation: Mapped[str | None] = mapped_column(String(8))  # approve | reject
    recommendation_reason: Mapped[str | None] = mapped_column(String(500))
    recommended_at: Mapped[datetime | None] = mapped_column(TS)
    decision_reason: Mapped[str | None] = mapped_column(String(500))
    decided_by: Mapped[str | None] = mapped_column(String(64))
    decided_at: Mapped[datetime | None] = mapped_column(TS)
    submitted_at: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, default=utcnow)


class LoanEvent(Base):
    """Append-only audit trail (application role: INSERT/SELECT only)."""

    __tablename__ = "loan_events"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    application_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    actor: Mapped[str] = mapped_column(String(80), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str | None] = mapped_column(String(16))
    details: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, default=utcnow)


class SummaryTask(Base):
    """Background summary of authorised core data for one application (synthetic demo scoring)."""

    __tablename__ = "summary_tasks"
    __table_args__ = (
        CheckConstraint("status IN ('queued','running','completed','failed')", name="ck_summary_status"),
        Index("ix_summary_status_not_before", "status", "not_before"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    application_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    batch_id: Mapped[str | None] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    not_before: Mapped[datetime | None] = mapped_column(TS)
    lease_owner: Mapped[str | None] = mapped_column(String(80))
    lease_expires_at: Mapped[datetime | None] = mapped_column(TS)
    result: Mapped[dict | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(TS)


class LoanBatch(Base):
    """Batch refresh of summaries. The member manifest (summary_tasks with this batch_id) is
    frozen in the SAME transaction that creates the batch."""

    __tablename__ = "loan_batches"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_loan_batch_idempotency"),
        CheckConstraint("status IN ('running','completed','completed_with_failures')", name="ck_loan_batch_status"),
        CheckConstraint("trigger_kind IN ('manual','schedule')", name="ck_loan_batch_trigger"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="running")
    trigger_kind: Mapped[str] = mapped_column(String(10), nullable=False)
    created_by: Mapped[str] = mapped_column(String(80), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    total_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_rounds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(TS)


class OutboxMessage(Base):
    __tablename__ = "outbox_messages"
    __table_args__ = (Index("ix_loans_outbox_unpublished", "published_at", "id"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    message_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    queue: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(TS)
    publish_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(200))


class RoleAssignment(RoleAssignmentMixin, Base):
    pass


class RoleEvent(RoleEventMixin, Base):
    pass
