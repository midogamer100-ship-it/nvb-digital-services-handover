"""M2 end-to-end (backend): browser session -> statements-api -> outbox/relay -> worker ->
Core Adapter -> patched core (/internal/v1) -> PDF -> private store -> authorised download.

Everything runs in-process: core on a temporary SQLite file, in-memory broker and object store,
test-only credentials generated per run. Not covered here: real RabbitMQ/S3/PostgreSQL, the
HTTP gateway and the React portal (see docs/TESTING.md).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import secrets
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

CORE_DIR = os.environ.get("CORE_BACKEND_DIR")
REPO = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(not CORE_DIR, reason="CORE_BACKEND_DIR not set")
PASSWORDS = {"customer": "customer-pass-123", "loadtest": "loadtest-pass-123",
             "demo": "demo-pass-123456", "admin": "admin-pass-123456"}


def _d(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


@pytest.fixture
def stack(tmp_path, monkeypatch):
    for k in list(os.environ):
        if k.upper().startswith(("NVB_", "ADAPTER_", "STATEMENTS_", "LOANS_", "KUBERNETES_")):
            monkeypatch.delenv(k)
    for p in (CORE_DIR, str(REPO / "services" / "core-adapter"), str(REPO / "services" / "statements"),
              str(REPO / "libs" / "nvb_platform")):
        monkeypatch.syspath_prepend(p)

    from fastapi.testclient import TestClient

    from app.config import Settings as CoreSettings
    from app.db import Base as CoreBase
    from app.main import create_app as create_core
    from app.services.seed import seed_database
    from core_adapter import credentials
    from core_adapter.app import create_app as create_adapter
    from core_adapter.config import Settings as AdapterSettings
    from nvb_statements.adapter_client import AdapterClient
    from nvb_statements.api import create_app as create_statements
    from nvb_statements.broker import InMemoryBroker
    from nvb_statements.config import Settings as StatementsSettings
    from nvb_statements.db import build_engine, build_sessionmaker
    from nvb_statements.models import Base
    from nvb_statements.storage import InMemoryStore
    from nvb_statements.worker import Worker

    monkeypatch.setattr(credentials, "RELOAD_CHECK_SECONDS", 0)
    core_tok, api_tok, worker_tok = (secrets.token_urlsafe(32) for _ in range(3))
    (tmp_path / "core-clients.json").write_text(json.dumps({"clients": [{
        "client_id": "nvb-core-adapter", "token_sha256": [_d(core_tok)],
        "scopes": ["identity:introspect", "accounts:read", "statements:extract"]}]}))
    core = create_core(CoreSettings(
        database_url=f"sqlite:///{tmp_path / 'core.db'}", cookie_secure=False, argon2_time_cost=1,
        argon2_memory_cost_kib=1024, log_level="WARNING", integration_enabled=True,
        integration_clients_file=str(tmp_path / "core-clients.json"), integration_settle_seconds=300))
    CoreBase.metadata.create_all(core.state.engine)
    db = core.state.sessionmaker()
    seed_database(db, core.state.passwords, PASSWORDS, customer_count=3, loadtest_count=0, demo_count=0)
    db.close()

    (tmp_path / "core-token").write_text(core_tok)
    (tmp_path / "adapter-clients.json").write_text(json.dumps({"clients": [
        {"client_id": "statements-api", "token_sha256": [_d(api_tok)], "scopes": ["identity:resolve", "accounts:read"]},
        {"client_id": "statements-worker", "token_sha256": [_d(worker_tok)], "scopes": ["statements:read"]}]}))
    adapter = create_adapter(AdapterSettings(
        core_base_url="http://core.inprocess", allow_insecure_http=True, core_token_file=str(tmp_path / "core-token"),
        clients_file=str(tmp_path / "adapter-clients.json"), log_level="WARNING"),
        core_transport=httpx.ASGITransport(app=core))
    adapter_transport = TestClient(adapter)._transport  # sync transport into the ASGI adapter

    (tmp_path / "api-token").write_text(api_tok)
    (tmp_path / "worker-token").write_text(worker_tok)
    st = StatementsSettings(database_url=f"sqlite:///{tmp_path / 'statements.db'}", allow_sqlite=True,
                            settle_seconds=600,
                            adapter_base_url="http://adapter.inprocess", log_level="WARNING")
    engine = build_engine(st)
    Base.metadata.create_all(engine)
    sm = build_sessionmaker(engine)
    store, broker = InMemoryStore(), InMemoryBroker()
    api = create_statements(st.model_copy(update={"adapter_token_file": str(tmp_path / "api-token")}),
                            adapter=AdapterClient(st.model_copy(update={"adapter_token_file": str(tmp_path / "api-token")}),
                                                  transport=adapter_transport),
                            store=store, sessionmaker=sm)
    worker = Worker(st, sm, broker, store,
                    AdapterClient(st.model_copy(update={"adapter_token_file": str(tmp_path / "worker-token")}),
                                  transport=adapter_transport), worker_id="e2e-worker")
    yield {"core": core, "api": api, "worker": worker, "sm": sm, "broker": broker, "store": store, "st": st}
    core.state.engine.dispose()
    engine.dispose()


def _login(core, username):
    from fastapi.testclient import TestClient

    c = TestClient(core)
    body = c.post("/api/auth/login", json={"username": username, "password": PASSWORDS["customer"]}).json()
    return c, c.cookies.get("nvb_session"), body["csrf_token"]


def _portal(api, session, csrf):
    from fastapi.testclient import TestClient

    c = TestClient(api)
    c.cookies.set("nvb_session", session)   # same origin via the gateway in deployment
    c.headers["X-CSRF-Token"] = csrf
    return c


def test_one_statement_end_to_end(stack):
    from pypdf import PdfReader
    from sqlalchemy import case, func, select

    from app.models import Account, LedgerEntry
    from nvb_statements.models import StatementRequest
    from nvb_statements.relay import publish_pending

    core_browser, session, csrf = _login(stack["core"], "customer001")
    accounts = core_browser.get("/api/accounts").json()["accounts"]
    acct = accounts[0]
    portal = _portal(stack["api"], session, csrf)

    r = portal.post("/x/statements/v1/requests", json={"account_id": acct["id"], "period": "2026-10"},
                    headers={"Idempotency-Key": uuid.uuid4().hex})
    assert r.status_code == 202, r.text
    rid = r.json()["id"]
    assert portal.get(f"/x/statements/v1/requests/{rid}").json()["status"] == "queued"

    assert publish_pending(stack["sm"], stack["broker"], 10) == 1
    d = stack["broker"].get(stack["st"].queue_name, 1)
    assert stack["worker"].handle(d) == "completed"

    status = portal.get(f"/x/statements/v1/requests/{rid}").json()
    assert status["status"] == "completed" and status["document_url"]
    pdf = portal.get(status["document_url"])
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf.content)).pages)
    assert acct["last4"] in text and "SYNTHETIC DEMO DATA" in text

    # Independent check of the historical closing balance straight from the core ledger.
    with stack["sm"]() as db:
        req = db.get(StatementRequest, rid)
    end = req.period_end_utc if req.period_end_utc.tzinfo else req.period_end_utc.replace(tzinfo=UTC)
    cdb = stack["core"].state.sessionmaker()
    signed = case((LedgerEntry.direction == "credit", LedgerEntry.amount_minor), else_=-LedgerEntry.amount_minor)
    closing = cdb.scalar(select(func.coalesce(func.sum(signed), 0))
                         .where(LedgerEntry.account_id == acct["id"], LedgerEntry.posted_at < end))
    count = cdb.scalar(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.account_id == acct["id"], LedgerEntry.posted_at < end,
        LedgerEntry.posted_at >= (req.period_start_utc if req.period_start_utc.tzinfo
                                  else req.period_start_utc.replace(tzinfo=UTC))))
    cdb.close()
    from nvb_statements.render import fmt_minor
    assert f"Closing balance {fmt_minor(int(closing), 'EGP')}" in text.replace("\n", " ")
    assert req.entry_count == count

    # Another customer cannot see or download it.
    _b, s2, c2 = _login(stack["core"], "customer002")
    other = _portal(stack["api"], s2, c2)
    assert other.get(f"/x/statements/v1/requests/{rid}").status_code == 404
    assert other.get(status["document_url"]).status_code == 404

    # Logout at the core is enforced at the extension's next check.
    core_browser.headers["X-CSRF-Token"] = csrf
    assert core_browser.post("/api/auth/logout").status_code == 200
    assert portal.get(status["document_url"]).status_code == 401


def test_current_month_is_refused_until_closed(stack):
    _c, session, csrf = _login(stack["core"], "customer001")
    acct = _c.get("/api/accounts").json()["accounts"][0]
    portal = _portal(stack["api"], session, csrf)
    now = datetime.now(UTC)
    r = portal.post("/x/statements/v1/requests", json={"account_id": acct["id"], "period": f"{now:%Y-%m}"},
                    headers={"Idempotency-Key": uuid.uuid4().hex})
    assert r.status_code == 422 and r.json()["error"]["code"] == "period_not_closed"
