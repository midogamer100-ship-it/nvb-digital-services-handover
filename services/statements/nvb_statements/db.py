"""Engine/session helpers for the statements database (shared implementation in ``nvb_platform``)."""

from __future__ import annotations

from sqlalchemy import Engine

from nvb_platform.db import as_utc, build_sessionmaker  # noqa: F401
from nvb_platform.db import build_engine as _build_engine
from nvb_statements.config import Settings


def build_engine(settings: Settings, application_name: str = "nvb-statements") -> Engine:
    return _build_engine(settings.resolved_database_url(), application_name=application_name,
                         pool_size=settings.db_pool_size, statement_timeout_ms=settings.db_statement_timeout_ms)
