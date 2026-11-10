"""Alembic environment for the statements database. Credentials come from files, never argv."""

from __future__ import annotations

import os
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

url = context.config.attributes.get("url") or os.environ.get("STATEMENTS_MIGRATION_DATABASE_URL")
if not url:
    raise SystemExit("STATEMENTS_MIGRATION_DATABASE_URL is required")
url = make_url(url)
pw_file = os.environ.get("STATEMENTS_MIGRATION_PASSWORD_FILE")
if pw_file:
    url = url.set(password=Path(pw_file).read_text(encoding="utf-8").strip())

engine = create_engine(url)
with engine.connect() as connection:
    context.configure(connection=connection, version_table="alembic_version_statements",
                      transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()
