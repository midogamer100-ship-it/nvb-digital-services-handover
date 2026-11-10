"""Statements configuration (prefix ``STATEMENTS_``). Secrets are always referenced by file path.

There is NO implicit database: ``STATEMENTS_DATABASE_URL`` must be set. SQLite is accepted only
with ``STATEMENTS_ALLOW_SQLITE=true`` (unit tests / laptop) and never inside Kubernetes.
"""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from nvb_platform.db import ConfigError, resolve_database_url
from nvb_platform.secrets import read_secret  # noqa: F401  (re-exported for callers)

__all__ = ["Settings", "get_settings", "read_secret", "ConfigError"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STATEMENTS_", env_file=None, extra="ignore")

    # Extension database (never the core DB). A URL *without* password plus a password file.
    database_url: str | None = None
    database_password_file: str | None = None
    allow_sqlite: bool = False
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_statement_timeout_ms: int = Field(default=15000, ge=100, le=120000)

    # Core adapter
    adapter_base_url: str = "https://core-adapter:8443"
    adapter_token_file: str | None = None
    adapter_ca_file: str | None = None
    adapter_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    # Plain http to the adapter is refused unless explicitly allowed (local development only).
    allow_insecure_http: bool = False
    # amqp:// (broker) and http:// (S3) are refused unless explicitly allowed. Meant for the
    # disposable test namespace / laptop only; the shared PoC uses amqps:// and https://.
    allow_plaintext_dependencies: bool = False

    # Broker (RabbitMQ). URL without credentials; credentials via files.
    broker_url: str | None = None  # e.g. amqps://rabbitmq.example:5671/nvb
    broker_username_file: str | None = None
    broker_password_file: str | None = None
    broker_ca_file: str | None = None
    # Must exceed the longest task: a blocking consumer does not service heartbeats while it
    # renders, so a short heartbeat would make the broker drop the connection mid-task.
    broker_heartbeat_seconds: int = Field(default=600, ge=0, le=3600)
    # Interactive customer requests and batch members use SEPARATE queues; the worker always
    # prefers the interactive queue so a large batch cannot starve a customer.
    queue_name: str = "nvb.statements.generate"
    batch_queue_name: str = "nvb.statements.batch"

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
    # Must be <= the adapter's ADAPTER_ENTRIES_MAX_PAGE_SIZE and the core's NVB_INTEGRATION_MAX_PAGE_SIZE.
    entries_page_size: int = Field(default=1000, ge=10, le=5000)
    # Must be <= the core's NVB_INTEGRATION_MAX_STATEMENT_ENTRIES.
    max_entries_per_statement: int = Field(default=200_000, ge=100)
    idle_exit_seconds: float = Field(default=30.0, ge=0, le=3600)
    # Bounds of one finite worker run (Kubernetes Job / KEDA ScaledJob). 0 = unbounded.
    worker_max_messages: int = Field(default=50, ge=0, le=100000)
    worker_max_runtime_seconds: int = Field(default=900, ge=0, le=86400)

    # Relay
    relay_batch_size: int = Field(default=100, ge=1, le=1000)
    relay_poll_seconds: float = Field(default=1.0, gt=0, le=60)
    stale_requeue_seconds: int = Field(default=900, ge=30, le=86400)

    # Batch orchestration
    batch_max_accounts: int = Field(default=5000, ge=1, le=200000)
    batch_discovery_page_size: int = Field(default=200, ge=1, le=500)
    batch_release_chunk: int = Field(default=200, ge=1, le=1000)
    batch_max_retry_rounds: int = Field(default=3, ge=0, le=20)

    log_level: str = "INFO"
    build_revision: str = "unknown"

    def resolved_database_url(self):
        return resolve_database_url(self.database_url, self.database_password_file,
                                    env_name="STATEMENTS_DATABASE_URL", allow_sqlite=self.allow_sqlite)

    def validate_runtime(self, component: str) -> list[str]:
        """Configuration problems for ``component`` (api | relay | worker | batch).

        Used at process start (workers/relay exit non-zero) and by API readiness. Messages
        name settings, never values.
        """
        problems: list[str] = []
        try:
            self.resolved_database_url()
        except ConfigError as exc:
            problems.append(str(exc))
        if component in ("api", "worker", "batch"):
            scheme = urlsplit(self.adapter_base_url).scheme
            if scheme == "http" and not self.allow_insecure_http:
                problems.append("STATEMENTS_ADAPTER_BASE_URL uses http:// without STATEMENTS_ALLOW_INSECURE_HTTP")
            elif scheme not in ("http", "https"):
                problems.append("STATEMENTS_ADAPTER_BASE_URL must be an http(s) URL")
            if not read_secret(self.adapter_token_file):
                problems.append("STATEMENTS_ADAPTER_TOKEN_FILE is missing or empty")
        if component in ("relay", "worker"):
            if not self.broker_url:
                problems.append("STATEMENTS_BROKER_URL is not set")
            elif urlsplit(self.broker_url).username or urlsplit(self.broker_url).password:
                problems.append("STATEMENTS_BROKER_URL must not embed credentials")
            elif urlsplit(self.broker_url).scheme == "amqp" and not self.allow_plaintext_dependencies:
                problems.append("STATEMENTS_BROKER_URL uses amqp:// without STATEMENTS_ALLOW_PLAINTEXT_DEPENDENCIES")
            elif urlsplit(self.broker_url).scheme not in ("amqp", "amqps"):
                problems.append("STATEMENTS_BROKER_URL must be an amqp(s) URL")
            if not read_secret(self.broker_username_file) or not read_secret(self.broker_password_file):
                problems.append("STATEMENTS_BROKER_USERNAME_FILE / STATEMENTS_BROKER_PASSWORD_FILE missing or empty")
        if component in ("api", "worker"):
            if not self.s3_endpoint_url:
                problems.append("STATEMENTS_S3_ENDPOINT_URL is not set")
            elif self.s3_endpoint_url.startswith("http://") and not self.allow_plaintext_dependencies:
                problems.append("STATEMENTS_S3_ENDPOINT_URL uses http:// without "
                                "STATEMENTS_ALLOW_PLAINTEXT_DEPENDENCIES")
            elif not self.s3_endpoint_url.startswith(("http://", "https://")):
                problems.append("STATEMENTS_S3_ENDPOINT_URL must be an http(s) URL")
            if not read_secret(self.s3_access_key_file) or not read_secret(self.s3_secret_key_file):
                problems.append("STATEMENTS_S3_ACCESS_KEY_FILE / STATEMENTS_S3_SECRET_KEY_FILE missing or empty")
        if self.queue_name == self.batch_queue_name:
            problems.append("STATEMENTS_QUEUE_NAME and STATEMENTS_BATCH_QUEUE_NAME must differ")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()
