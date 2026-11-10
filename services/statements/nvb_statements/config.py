"""Statements configuration (prefix ``STATEMENTS_``). Secrets are always referenced by file path."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def read_secret(path: str | None) -> str | None:
    if not path:
        return None
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STATEMENTS_", env_file=None, extra="ignore")

    # Extension database (never the core DB). A URL *without* password plus a password file.
    database_url: str = "sqlite:///./statements-dev.db"
    database_password_file: str | None = None
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_statement_timeout_ms: int = Field(default=15000, ge=100, le=120000)

    # Core adapter
    adapter_base_url: str = "http://core-adapter:8080"
    adapter_token_file: str | None = None
    adapter_ca_file: str | None = None
    adapter_timeout_seconds: float = Field(default=15.0, gt=0, le=120)

    # Broker (RabbitMQ). URL without credentials; credentials via files.
    broker_url: str | None = None  # e.g. amqps://rabbitmq.nvb-digital.svc:5671/nvb
    broker_username_file: str | None = None
    broker_password_file: str | None = None
    broker_ca_file: str | None = None
    queue_name: str = "nvb.statements.generate"

    # Object storage (S3 compatible, private bucket)
    s3_endpoint_url: str | None = None
    s3_bucket: str = "nvb-statements"
    s3_region: str = "us-east-1"
    s3_access_key_file: str | None = None
    s3_secret_key_file: str | None = None
    s3_ca_file: str | None = None

    # Business rules
    statement_timezone: str = "Africa/Cairo"
    # Must be >= the core's NVB_INTEGRATION_SETTLE_SECONDS.
    settle_seconds: int = Field(default=600, ge=0, le=86400)
    max_months_back: int = Field(default=13, ge=1, le=120)
    download_max_bytes: int = Field(default=50 * 1024 * 1024, ge=1024)

    # Worker
    worker_id: str | None = None
    lease_seconds: int = Field(default=300, ge=10, le=3600)
    max_attempts: int = Field(default=5, ge=1, le=20)
    retry_base_seconds: int = Field(default=30, ge=0, le=3600)
    retry_max_seconds: int = Field(default=900, ge=0, le=86400)
    entries_page_size: int = Field(default=1000, ge=10, le=5000)
    max_entries_per_statement: int = Field(default=200_000, ge=100)
    idle_exit_seconds: float = Field(default=30.0, ge=0, le=3600)

    # Relay
    relay_batch_size: int = Field(default=100, ge=1, le=1000)
    relay_poll_seconds: float = Field(default=1.0, gt=0, le=60)

    log_level: str = "INFO"
    build_revision: str = "unknown"

    def resolved_database_url(self):
        from sqlalchemy.engine import make_url

        url = make_url(self.database_url)
        password = read_secret(self.database_password_file)
        return url.set(password=password) if password else url


@lru_cache
def get_settings() -> Settings:
    return Settings()
