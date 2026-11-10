"""Engine/session helpers shared by the extension services (each service has its OWN database).

PostgreSQL is the deployment database. SQLite exists only for unit tests / laptop
development and must be enabled explicitly (``allow_sqlite``); it is always refused inside
Kubernetes, so a missing database setting can never silently fall back to a local file.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session, sessionmaker

from nvb_platform.secrets import read_secret


class ConfigError(RuntimeError):
    """Invalid or unsafe runtime configuration. The message never contains secret values."""


def as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def utcnow() -> datetime:
    return datetime.now(UTC)


def resolve_database_url(database_url: str | None, password_file: str | None, *, env_name: str,
                         allow_sqlite: bool) -> URL:
    if not database_url:
        raise ConfigError(f"{env_name} is not set")
    url = make_url(database_url)
    backend = url.get_backend_name()
    if backend == "sqlite":
        if os.environ.get("KUBERNETES_SERVICE_HOST"):
            raise ConfigError(f"{env_name} points at SQLite inside Kubernetes; PostgreSQL is required")
        if not allow_sqlite:
            raise ConfigError(f"{env_name} points at SQLite; set the explicit development flag to allow it")
        return url
    if backend != "postgresql":
        raise ConfigError(f"{env_name} must be a postgresql+psycopg URL")
    if url.password:
        raise ConfigError(f"{env_name} must not embed a password; use the password file setting")
    password = read_secret(password_file)
    if not password:
        raise ConfigError(f"database password file for {env_name} is missing or empty")
    return url.set(password=password)


def build_engine(url: URL, *, application_name: str, pool_size: int = 5,
                 statement_timeout_ms: int = 15000) -> Engine:
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
        url, pool_size=pool_size, max_overflow=2, pool_pre_ping=True, pool_recycle=1800,
        connect_args={"connect_timeout": 5, "application_name": application_name,
                      "options": f"-c statement_timeout={statement_timeout_ms}"})


def build_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
