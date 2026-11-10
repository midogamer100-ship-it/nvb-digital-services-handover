"""Loans initial schema.

Revision ID: 0001_loans_initial
Revises: None

Compatibility: additive only. Application images >= 0.1.0 require this revision.
Rollback: an image rollback never needs a schema downgrade for this revision; automatic
downgrade is disabled (dropping tables would destroy applications and the audit trail).
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0001_loans_initial"
down_revision = None
branch_labels = None
depends_on = None

PK = sa.BigInteger().with_variant(sa.Integer, "sqlite")
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "loan_applications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_subject", sa.String(64), nullable=False),
        sa.Column("owner_label", sa.String(80), nullable=False),
        sa.Column("account_id", sa.BigInteger, nullable=False),
        sa.Column("account_masked", sa.String(16), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("amount_minor", sa.BigInteger, nullable=False),
        sa.Column("term_months", sa.Integer, nullable=False),
        sa.Column("monthly_income_minor", sa.BigInteger, nullable=False),
        sa.Column("purpose", sa.String(200), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("reviewer_subject", sa.String(64), index=True),
        sa.Column("assigned_by", sa.String(64)),
        sa.Column("recommendation", sa.String(8)),
        sa.Column("recommendation_reason", sa.String(500)),
        sa.Column("recommended_at", TS),
        sa.Column("decision_reason", sa.String(500)),
        sa.Column("decided_by", sa.String(64)),
        sa.Column("decided_at", TS),
        sa.Column("submitted_at", TS),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.CheckConstraint("status IN ('draft','submitted','under_review','approved','rejected')", name="ck_loan_status"),
        sa.CheckConstraint("amount_minor > 0 AND term_months > 0 AND monthly_income_minor >= 0", name="ck_loan_values"),
    )
    op.create_index("ix_loan_owner", "loan_applications", ["owner_subject", "created_at"])
    op.create_index("ix_loan_status", "loan_applications", ["status"])

    op.create_table(
        "loan_events",
        sa.Column("id", PK, primary_key=True, autoincrement=True),
        sa.Column("application_id", sa.String(36), nullable=False, index=True),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("actor", sa.String(80), nullable=False),
        sa.Column("from_status", sa.String(16)),
        sa.Column("to_status", sa.String(16)),
        sa.Column("details", sa.JSON, nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_table(
        "summary_tasks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("application_id", sa.String(36), nullable=False, index=True),
        sa.Column("batch_id", sa.String(36), index=True),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("not_before", TS),
        sa.Column("lease_owner", sa.String(80)),
        sa.Column("lease_expires_at", TS),
        sa.Column("result", sa.JSON),
        sa.Column("error_code", sa.String(40)),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("completed_at", TS),
        sa.CheckConstraint("status IN ('queued','running','completed','failed')", name="ck_summary_status"),
    )
    op.create_index("ix_summary_status_not_before", "summary_tasks", ["status", "not_before"])
    op.create_table(
        "loan_batches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("trigger_kind", sa.String(10), nullable=False),
        sa.Column("created_by", sa.String(80), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("total_items", sa.Integer, nullable=False),
        sa.Column("retry_rounds", sa.Integer, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("finished_at", TS),
        sa.UniqueConstraint("idempotency_key", name="uq_loan_batch_idempotency"),
        sa.CheckConstraint("status IN ('running','completed','completed_with_failures')", name="ck_loan_batch_status"),
        sa.CheckConstraint("trigger_kind IN ('manual','schedule')", name="ck_loan_batch_trigger"),
    )
    op.create_table(
        "outbox_messages",
        sa.Column("id", PK, primary_key=True, autoincrement=True),
        sa.Column("message_id", sa.String(36), nullable=False, unique=True),
        sa.Column("queue", sa.String(80), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("published_at", TS),
        sa.Column("publish_attempts", sa.Integer, nullable=False),
        sa.Column("last_error", sa.String(200)),
    )
    op.create_index("ix_loans_outbox_unpublished", "outbox_messages", ["published_at", "id"])
    op.create_table(
        "role_assignments",
        sa.Column("id", PK, primary_key=True, autoincrement=True),
        sa.Column("subject", sa.String(64), nullable=False, index=True),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("active", sa.Boolean, nullable=False),
        sa.Column("label", sa.String(80), nullable=False),
        sa.Column("updated_by", sa.String(80), nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.UniqueConstraint("subject", "role", name="uq_role_subject_role"),
    )
    op.create_table(
        "role_events",
        sa.Column("id", PK, primary_key=True, autoincrement=True),
        sa.Column("subject", sa.String(64), nullable=False, index=True),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("action", sa.String(8), nullable=False),
        sa.Column("actor", sa.String(80), nullable=False),
        sa.Column("reason", sa.String(200), nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )

    app_role = os.environ.get("LOANS_APP_ROLE")
    if app_role and op.get_bind().dialect.name == "postgresql":
        r = op.get_bind().dialect.identifier_preparer.quote(app_role)
        op.execute("GRANT SELECT, INSERT, UPDATE ON loan_applications, summary_tasks, loan_batches, "
                   f"outbox_messages, role_assignments TO {r}")
        op.execute(f"GRANT SELECT, INSERT ON loan_events, role_events TO {r}")  # append-only audit
        op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {r}")


def downgrade() -> None:
    raise RuntimeError("Automatic downgrade is disabled; see docs/RUNBOOK.md")
