"""Loans configuration (prefix ``LOANS_``). Secrets are always referenced by file path.

No implicit database: ``LOANS_DATABASE_URL`` must be set; SQLite needs ``LOANS_ALLOW_SQLITE=true``
and is refused inside Kubernetes.
"""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from nvb_platform.db import ConfigError, resolve_database_url
from nvb_platform.secrets import read_secret


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LOANS_", env_file=None, extra="ignore")

    database_url: str | None = None
    database_password_file: str | None = None
    allow_sqlite: bool = False
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_statement_timeout_ms: int = Field(default=15000, ge=100, le=120000)

    adapter_base_url: str = "https://core-adapter:8443"
    adapter_token_file: str | None = None
    adapter_ca_file: str | None = None
    adapter_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    allow_insecure_http: bool = False
    allow_plaintext_dependencies: bool = False

    broker_url: str | None = None
    broker_username_file: str | None = None
    broker_password_file: str | None = None
    broker_ca_file: str | None = None
    broker_heartbeat_seconds: int = Field(default=600, ge=0, le=3600)
    queue_name: str = "nvb.loans.summarize"   # separate from the statements queues

    # Business rules (demo)
    summary_timezone: str = "Africa/Cairo"
    summary_months: int = Field(default=3, ge=1, le=12)
    # Must be >= the core's NVB_INTEGRATION_SETTLE_SECONDS.
    settle_seconds: int = Field(default=600, ge=0, le=86400)
    max_amount_minor: int = Field(default=500_000_000, ge=1)   # 5,000,000.00
    max_term_months: int = Field(default=84, ge=1, le=480)

    worker_id: str | None = None
    lease_seconds: int = Field(default=120, ge=10, le=3600)
    max_attempts: int = Field(default=5, ge=1, le=20)
    retry_base_seconds: int = Field(default=30, ge=0, le=3600)
    retry_max_seconds: int = Field(default=900, ge=0, le=86400)
    idle_exit_seconds: float = Field(default=30.0, ge=0, le=3600)
    worker_max_messages: int = Field(default=50, ge=0, le=100000)
    worker_max_runtime_seconds: int = Field(default=600, ge=0, le=86400)

    relay_batch_size: int = Field(default=100, ge=1, le=1000)
    relay_poll_seconds: float = Field(default=1.0, gt=0, le=60)
    stale_requeue_seconds: int = Field(default=900, ge=30, le=86400)

    batch_max_applications: int = Field(default=5000, ge=1, le=200000)
    batch_max_retry_rounds: int = Field(default=3, ge=0, le=20)

    log_level: str = "INFO"
    build_revision: str = "unknown"

    def resolved_database_url(self):
        return resolve_database_url(self.database_url, self.database_password_file,
                                    env_name="LOANS_DATABASE_URL", allow_sqlite=self.allow_sqlite)

    def validate_runtime(self, component: str) -> list[str]:
        """Problems for ``component`` (api | relay | worker). Names settings, never values."""
        problems: list[str] = []
        try:
            self.resolved_database_url()
        except ConfigError as exc:
            problems.append(str(exc))
        if component in ("api", "worker"):
            scheme = urlsplit(self.adapter_base_url).scheme
            if scheme == "http" and not self.allow_insecure_http:
                problems.append("LOANS_ADAPTER_BASE_URL uses http:// without LOANS_ALLOW_INSECURE_HTTP")
            elif scheme not in ("http", "https"):
                problems.append("LOANS_ADAPTER_BASE_URL must be an http(s) URL")
            if not read_secret(self.adapter_token_file):
                problems.append("LOANS_ADAPTER_TOKEN_FILE is missing or empty")
        if component in ("relay", "worker"):
            parts = urlsplit(self.broker_url or "")
            if not self.broker_url:
                problems.append("LOANS_BROKER_URL is not set")
            elif parts.username or parts.password:
                problems.append("LOANS_BROKER_URL must not embed credentials")
            elif parts.scheme == "amqp" and not self.allow_plaintext_dependencies:
                problems.append("LOANS_BROKER_URL uses amqp:// without LOANS_ALLOW_PLAINTEXT_DEPENDENCIES")
            elif parts.scheme not in ("amqp", "amqps"):
                problems.append("LOANS_BROKER_URL must be an amqp(s) URL")
            if not read_secret(self.broker_username_file) or not read_secret(self.broker_password_file):
                problems.append("LOANS_BROKER_USERNAME_FILE / LOANS_BROKER_PASSWORD_FILE missing or empty")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()
