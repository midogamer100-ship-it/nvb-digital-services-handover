"""Engine/session helpers for the statements database."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from nvb_statements.config import Settings


def as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def build_engine(settings: Settings) -> Engine:
    url = settings.resolved_database_url()
    if url.get_backend_name() == "sqlite":
        engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(engine, "connect")
        def _c(conn, _r):  # pragma: no cover
            conn.isolation_level = None
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")

        @event.listens_for(engine, "begin")
        def _b(conn):  # pragma: no cover
            conn.exec_driver_sql("BEGIN IMMEDIATE")

        return engine
    return create_engine(
        url, pool_size=settings.db_pool_size, max_overflow=2, pool_pre_ping=True, pool_recycle=1800,
        connect_args={"connect_timeout": 5, "application_name": "nvb-statements",
                      "options": f"-c statement_timeout={settings.db_statement_timeout_ms}"})


def build_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
