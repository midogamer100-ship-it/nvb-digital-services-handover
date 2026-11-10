"""Contract test: the real Core Adapter against the real (patched) core application.

The core runs in-process on a temporary SQLite file with test-only credentials; the
adapter reaches it through httpx.ASGITransport. Nothing touches a network or a shared
database. Requires CORE_BACKEND_DIR to point at the patched core's backend/ directory;
skipped otherwise.

SQLite does not exercise PostgreSQL REPEATABLE READ / row locking: see docs/TESTING.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

CORE_DIR = os.environ.get("CORE_BACKEND_DIR")
REPO = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(not CORE_DIR, reason="CORE_BACKEND_DIR not set")

TEST_PASSWORDS = {"customer": "customer-pass-123", "loadtest": "loadtest-pass-123",
                  "demo": "demo-pass-123456", "admin": "admin-pass-123456"}


def _digest(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


@pytest.fixture
def stack(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.upper().startswith(("NVB_", "ADAPTER_")):
            monkeypatch.delenv(key)
    monkeypatch.syspath_prepend(CORE_DIR)
    monkeypatch.syspath_prepend(str(REPO / "services" / "core-adapter"))

    from app.config import Settings as CoreSettings  # core
    from app.db import Base
    from app.main import create_app as create_core
    from app.services.seed import seed_database

    from core_adapter import credentials
    from core_adapter.app import create_app as create_adapter
    from core_adapter.config import Settings as AdapterSettings

    monkeypatch.setattr(credentials, "RELOAD_CHECK_SECONDS", 0)
    core_token, svc_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    (tmp_path / "core-clients.json").write_text(json.dumps({"clients": [{
        "client_id": "nvb-core-adapter", "token_sha256": [_digest(core_token)],
        "scopes": ["identity:introspect", "accounts:read", "statements:extract", "summaries:read",
                   "accounts:discover"]}]}))
    core_settings = CoreSettings(
        database_url=f"sqlite:///{tmp_path / 'core.db'}", cookie_secure=False, argon2_time_cost=1,
        argon2_memory_cost_kib=1024, log_level="WARNING", integration_enabled=True,
        integration_clients_file=str(tmp_path / "core-clients.json"), integration_settle_seconds=0)
    core = create_core(core_settings)
    Base.metadata.create_all(core.state.engine)
    db = core.state.sessionmaker()
    seed_database(db, core.state.passwords, TEST_PASSWORDS, customer_count=3, loadtest_count=1, demo_count=0)
    db.close()

    (tmp_path / "core-token").write_text(core_token)
    (tmp_path / "adapter-clients.json").write_text(json.dumps({"clients": [{
        "client_id": "statements-api", "token_sha256": [_digest(svc_token)],
        "scopes": ["identity:resolve", "accounts:read", "statements:read", "discovery:read"]}]}))
    adapter = create_adapter(
        AdapterSettings(core_base_url="http://core.inprocess", allow_insecure_http=True,
                        core_token_file=str(tmp_path / "core-token"),
                        clients_file=str(tmp_path / "adapter-clients.json"), log_level="WARNING"),
        core_transport=httpx.ASGITransport(app=core))
    yield core, adapter, svc_token
    core.state.engine.dispose()


def _browser_login(core, username="customer001"):
    from fastapi.testclient import TestClient

    c = TestClient(core)
    body = c.post("/api/auth/login", json={"username": username, "password": TEST_PASSWORDS["customer"]}).json()
    return c, c.cookies.get("nvb_session"), body["csrf_token"]


async def test_identity_statement_and_discovery_flow(stack):
    core, adapter, svc = stack
    browser, session, csrf = _browser_login(core)
    h = {"Authorization": f"Bearer {svc}", "X-Request-ID": "contract-test-0001"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=adapter), base_url="http://adapter") as a:
        ident = (await a.post("/v1/identity/resolve", headers=h,
                              json={"session_token": session, "csrf_token": csrf, "include_accounts": True})).json()
        assert ident["active"] and ident["csrf_valid"] is True and ident["role"] == "customer"
        acct = ident["accounts"][0]["account_id"]

        end = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=1)
        start = end - timedelta(days=90)
        params = {"subject": ident["subject"], "period_start": start.isoformat(), "period_end": end.isoformat()}
        m = (await a.get(f"/v1/statement-datasets/{acct}/manifest", headers=h, params=params)).json()
        items, cursor = [], None
        while True:
            q = {**params, "limit": 25, **({"cursor": cursor} if cursor else {})}
            page = (await a.get(f"/v1/statement-datasets/{acct}/entries", headers=h, params=q)).json()
            items += page["items"]
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]
        net = sum(i["signed_amount_minor"] for i in items)
        assert len(items) == m["entry_count"]
        assert m["opening_balance_minor"] + net == m["closing_balance_minor"]
        m2 = (await a.get(f"/v1/statement-datasets/{acct}/manifest", headers=h, params=params)).json()
        assert m2["dataset_id"] == m["dataset_id"]

        # Another customer's account through the same subject: 404, no existence leak.
        _b2, s2, _ = _browser_login(core, "customer002")
        other = (await a.post("/v1/identity/resolve", headers=h,
                              json={"session_token": s2, "include_accounts": True})).json()
        other_acct = other["accounts"][0]["account_id"]
        r = await a.get(f"/v1/statement-datasets/{other_acct}/manifest", headers=h, params=params)
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"

        disc = (await a.get("/v1/discovery/statement-accounts", headers=h, params={"limit": 2})).json()
        assert disc["items"] and all(i["subject"].startswith("nvb-user:") for i in disc["items"])

        # Logout through the core -> identity becomes inactive at the next check.
        browser.headers["X-CSRF-Token"] = csrf
        assert browser.post("/api/auth/logout").status_code == 200
        gone = (await a.post("/v1/identity/resolve", headers=h, json={"session_token": session})).json()
        assert gone == {"active": False}


async def test_adapter_with_wrong_core_credential_fails_closed(stack, tmp_path):
    core, adapter, svc = stack
    (tmp_path / "core-token").write_text(secrets.token_urlsafe(32))
    import os as _os
    import time as _t
    _os.utime(tmp_path / "core-token", (_t.time() + 30, _t.time() + 30))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=adapter), base_url="http://adapter") as a:
        r = await a.post("/v1/identity/resolve", headers={"Authorization": f"Bearer {svc}"},
                         json={"session_token": "whatever"})
    assert r.status_code == 502 and r.json()["error"]["code"] == "core_auth_misconfigured"
