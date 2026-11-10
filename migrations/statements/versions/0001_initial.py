"""Statements initial schema.

Revision ID: 0001_statements_initial
Revises: None

Compatibility: additive only. Application images >= 0.1.0 require this revision.
Rollback: application image rollback never needs a schema downgrade for this revision;
automatic downgrade is disabled (dropping tables would destroy request history).
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0001_statements_initial"
down_revision = None
branch_labels = None
depends_on = None

BIGINT_PK = sa.BigInteger().with_variant(sa.Integer, "sqlite")
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "statement_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_subject", sa.String(64), nullable=False),
        sa.Column("requested_by_subject", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(20), nullable=False),
        sa.Column("batch_id", sa.String(36), index=True),
        sa.Column("account_id", sa.BigInteger, nullable=False),
        sa.Column("account_masked", sa.String(16), nullable=False),
        sa.Column("data_scope", sa.String(40), nullable=False),
        sa.Column("period_label", sa.String(7), nullable=False),
        sa.Column("period_timezone", sa.String(40), nullable=False),
        sa.Column("period_start_utc", TS, nullable=False),
        sa.Column("period_end_utc", TS, nullable=False),
        sa.Column("dataset_version", sa.String(20), nullable=False),
        sa.Column("dataset_id", sa.String(64)),
        sa.Column("entry_count", sa.Integer),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("not_before", TS),
        sa.Column("lease_owner", sa.String(80)),
        sa.Column("lease_expires_at", TS),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("result_key", sa.String(200)),
        sa.Column("result_sha256", sa.String(64)),
        sa.Column("result_size", sa.BigInteger),
        sa.Column("error_code", sa.String(40)),
        sa.Column("error_message", sa.String(200)),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("started_at", TS),
        sa.Column("completed_at", TS),
        sa.UniqueConstraint("owner_subject", "idempotency_key", name="uq_statement_idempotency"),
        sa.CheckConstraint("status IN ('queued','running','completed','failed','cancelled')",
                           name="ck_statement_status"),
        sa.CheckConstraint("purpose IN ('customer_request','batch')", name="ck_statement_purpose"),
    )
    op.create_index("ix_statement_owner_created", "statement_requests", ["owner_subject", "created_at"])
    op.create_index("ix_statement_status_not_before", "statement_requests", ["status", "not_before"])

    op.create_table(
        "statement_events",
        sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
        sa.Column("request_id", sa.String(36), nullable=False, index=True),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("actor", sa.String(80), nullable=False),
        sa.Column("details", sa.JSON, nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_table(
        "outbox_messages",
        sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
        sa.Column("message_id", sa.String(36), nullable=False, unique=True),
        sa.Column("queue", sa.String(80), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("published_at", TS),
        sa.Column("publish_attempts", sa.Integer, nullable=False),
        sa.Column("last_error", sa.String(200)),
    )
    op.create_index("ix_outbox_unpublished", "outbox_messages", ["published_at", "id"])

    app_role = os.environ.get("STATEMENTS_APP_ROLE")
    if app_role and op.get_bind().dialect.name == "postgresql":
        role = sa.sql.quoted_name(app_role, quote=True)
        q = op.get_bind().dialect.identifier_preparer.quote
        r = q(str(role))
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON statement_requests, outbox_messages TO {r}")
        op.execute(f"GRANT SELECT, INSERT ON statement_events TO {r}")  # append-only audit
        op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {r}")


def downgrade() -> None:
    raise RuntimeError("Automatic downgrade is disabled; see docs/RUNBOOK.md")
