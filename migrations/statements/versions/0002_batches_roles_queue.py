"""Batches, extension role assignments and the per-request queue.

Revision ID: 0002_statements_batches_roles
Revises: 0001_statements_initial

Compatibility: additive only (new nullable-with-default column, new tables). Application images
0.1.x keep working against this revision (they ignore the new column: it has a server default).
Application images >= 0.2.0 REQUIRE this revision. Deploy order: run this migration first, then
roll out the 0.2.x images. Rolling the images back to 0.1.x does not need a schema downgrade.
Automatic downgrade is disabled (it would drop batch history and role assignments).
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0002_statements_batches_roles"
down_revision = "0001_statements_initial"
branch_labels = None
depends_on = None

BIGINT_PK = sa.BigInteger().with_variant(sa.Integer, "sqlite")
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column("statement_requests", sa.Column("queue", sa.String(80), nullable=False,
                                                  server_default="nvb.statements.generate"))

    op.create_table(
        "statement_batches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("period_label", sa.String(7), nullable=False),
        sa.Column("period_timezone", sa.String(40), nullable=False),
        sa.Column("period_start_utc", TS, nullable=False),
        sa.Column("period_end_utc", TS, nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("trigger_kind", sa.String(10), nullable=False),
        sa.Column("created_by", sa.String(80), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("include_load_test", sa.Boolean, nullable=False),
        sa.Column("discovery_cursor", sa.BigInteger, nullable=False),
        sa.Column("total_items", sa.Integer, nullable=False),
        sa.Column("released_items", sa.Integer, nullable=False),
        sa.Column("retry_rounds", sa.Integer, nullable=False),
        sa.Column("error_code", sa.String(40)),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("frozen_at", TS),
        sa.Column("finished_at", TS),
        sa.UniqueConstraint("idempotency_key", name="uq_batch_idempotency"),
        sa.CheckConstraint("status IN ('freezing','releasing','running','completed','completed_with_failures',"
                           "'cancelled','failed')", name="ck_batch_status"),
        sa.CheckConstraint("trigger_kind IN ('manual','schedule')", name="ck_batch_trigger"),
    )
    op.create_table(
        "batch_items",
        sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
        sa.Column("batch_id", sa.String(36), nullable=False, index=True),
        sa.Column("owner_subject", sa.String(64), nullable=False),
        sa.Column("account_id", sa.BigInteger, nullable=False),
        sa.Column("account_masked", sa.String(16), nullable=False),
        sa.Column("request_id", sa.String(36)),
        sa.UniqueConstraint("batch_id", "account_id", name="uq_batch_item_account"),
    )
    op.create_index("ix_batch_item_unreleased", "batch_items", ["batch_id", "request_id"])

    op.create_table(
        "role_assignments",
        sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
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
        sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
        sa.Column("subject", sa.String(64), nullable=False, index=True),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("action", sa.String(8), nullable=False),
        sa.Column("actor", sa.String(80), nullable=False),
        sa.Column("reason", sa.String(200), nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )

    app_role = os.environ.get("STATEMENTS_APP_ROLE")
    if app_role and op.get_bind().dialect.name == "postgresql":
        r = op.get_bind().dialect.identifier_preparer.quote(app_role)
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON statement_batches, batch_items, role_assignments TO {r}")
        op.execute(f"GRANT SELECT, INSERT ON role_events TO {r}")  # append-only audit
        op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {r}")


def downgrade() -> None:
    raise RuntimeError("Automatic downgrade is disabled; see docs/RUNBOOK.md")
