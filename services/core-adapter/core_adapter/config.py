"""Core Adapter configuration (environment prefix ``ADAPTER_``). No secret has a default."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ADAPTER_", env_file=None, extra="ignore")

    # Core endpoint: the infra-agreed load-balancer URL. Never hard-coded in code.
    core_base_url: str | None = None
    # File containing the adapter's core client token (mounted Secret). Re-read on change.
    core_token_file: str | None = None
    # PEM bundle used to verify the core's TLS certificate. Hostname verification is always on.
    core_ca_file: str | None = None
    # Plain http is refused unless explicitly allowed (local development only).
    allow_insecure_http: bool = False

    core_connect_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    core_read_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    core_max_connections: int = Field(default=4, ge=1, le=64)
    # Per-replica bound on concurrent core calls; fleet bound = replicas x this value.
    core_max_concurrency: int = Field(default=4, ge=1, le=64)
    core_queue_timeout_seconds: float = Field(default=2.0, ge=0, le=30)
    # Per-replica token bucket for core calls.
    core_rate_per_second: float = Field(default=20.0, gt=0, le=1000)
    core_rate_burst: int = Field(default=20, ge=1, le=1000)
    core_retry_attempts: int = Field(default=3, ge=1, le=5)
    core_retry_base_seconds: float = Field(default=0.2, ge=0, le=5)
    core_retry_max_seconds: float = Field(default=2.0, ge=0, le=30)

    # Inbound service credentials (same hashed-JSON format as the core integration file).
    clients_file: str | None = None

    entries_max_page_size: int = Field(default=1000, ge=10, le=5000)
    log_level: str = "INFO"
    instance_id: str | None = None
    build_revision: str = "unknown"

    @field_validator("core_base_url")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return v.rstrip("/") if v else v

    def validate_runtime(self) -> list[str]:
        """Return configuration problems (used by readiness). Never includes secret values."""
        problems = []
        if not self.core_base_url:
            problems.append("ADAPTER_CORE_BASE_URL is not set")
        elif self.core_base_url.startswith("http://") and not self.allow_insecure_http:
            problems.append("ADAPTER_CORE_BASE_URL uses http:// without ADAPTER_ALLOW_INSECURE_HTTP")
        elif not self.core_base_url.startswith(("http://", "https://")):
            problems.append("ADAPTER_CORE_BASE_URL must be an http(s) URL")
        if not self.core_token_file:
            problems.append("ADAPTER_CORE_TOKEN_FILE is not set")
        if not self.clients_file:
            problems.append("ADAPTER_CLIENTS_FILE is not set")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()
