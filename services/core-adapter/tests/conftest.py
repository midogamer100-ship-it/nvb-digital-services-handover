from __future__ import annotations

import hashlib
import json
import secrets

import httpx
import pytest

from core_adapter import credentials
from core_adapter.app import create_app
from core_adapter.config import Settings

FULL_SCOPES = ["identity:resolve", "accounts:read", "statements:read", "summaries:read", "discovery:read"]


def token() -> str:
    return secrets.token_urlsafe(32)


def write_clients(path, clients: dict[str, tuple[list[str], list[str]]]) -> None:
    path.write_text(json.dumps({"clients": [
        {"client_id": cid, "token_sha256": [hashlib.sha256(t.encode()).hexdigest() for t in toks], "scopes": sc}
        for cid, (toks, sc) in clients.items()]}), encoding="utf-8")


@pytest.fixture(autouse=True)
def no_reload_delay(monkeypatch):
    monkeypatch.setattr(credentials, "RELOAD_CHECK_SECONDS", 0)


@pytest.fixture
def tokens():
    return {"full": token(), "statements": token(), "identity": token(), "core": token()}


@pytest.fixture
def settings_factory(tmp_path, tokens):
    clients = tmp_path / "clients.json"
    write_clients(clients, {
        "statements-api": ([tokens["full"]], FULL_SCOPES),
        "statements-worker": ([tokens["statements"]], ["statements:read"]),
        "gateway": ([tokens["identity"]], ["identity:resolve"]),
    })
    core_token = tmp_path / "core-token"
    core_token.write_text(tokens["core"], encoding="utf-8")

    def make(**overrides) -> Settings:
        base = dict(core_base_url="https://core.test.invalid", core_token_file=str(core_token),
                    clients_file=str(clients), core_retry_base_seconds=0, core_retry_max_seconds=0,
                    core_queue_timeout_seconds=0.2, log_level="WARNING")
        base.update(overrides)
        return Settings(**base)

    return make


def adapter_client(settings: Settings, handler) -> httpx.AsyncClient:
    app = create_app(settings, core_transport=httpx.MockTransport(handler))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://adapter.test")


def auth(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}
