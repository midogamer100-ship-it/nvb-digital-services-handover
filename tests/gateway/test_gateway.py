"""Gateway + portal tests over REAL sockets: browser origin -> nginx gateway -> core / extension
APIs -> PostgreSQL, RabbitMQ, object storage (all disposable, started by scripts/local_run.py).

    python scripts/local_stack.py start && python scripts/local_run.py up
    python -m pytest tests/gateway -p no:cacheprovider -o addopts= --import-mode=importlib -q

Skipped unless the local stack answers on its loopback ports.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / ".local" / "run"
GW = "http://127.0.0.1:18080"
CORE = "http://127.0.0.1:18100"
S, L = "/x/statements/v1", "/x/loans/v1"
PY = sys.executable


def _up() -> bool:
    try:
        return httpx.get(f"{GW}/gateway/health", timeout=2).status_code == 200 and (RUN / "demo-passwords.json").exists()
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(not _up(), reason="local stack not running (scripts/local_run.py up)")


def passwords() -> dict:
    return json.loads((RUN / "demo-passwords.json").read_text())


def login(username: str, group: str = "customer") -> httpx.Client:
    c = httpx.Client(base_url=GW, timeout=30)
    r = c.post("/api/auth/login", json={"username": username, "password": passwords()[group]})
    assert r.status_code == 200, r.text
    c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    c.login_response = r
    return c


def local_run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([PY, str(ROOT / "scripts" / "local_run.py"), *args], capture_output=True, text=True,
                          cwd=str(ROOT), timeout=300)


def request_statement(c: httpx.Client, period: str = "2026-10", index: int = 0) -> str:
    accounts = c.get(f"{S}/accounts").json()["items"]
    r = c.post(f"{S}/requests", json={"account_id": accounts[index]["account_id"], "period": period},
               headers={"Idempotency-Key": uuid.uuid4().hex})
    assert r.status_code == 202, r.text
    return r.json()["id"]


def wait_status(c: httpx.Client, rid: str, wanted: tuple[str, ...], seconds: float = 60) -> dict:
    deadline = time.monotonic() + seconds
    while True:
        body = c.get(f"{S}/requests/{rid}").json()
        if body["status"] in wanted or time.monotonic() > deadline:
            return body
        time.sleep(0.5)


# --- login / session through the new origin ----------------------------------------------------


def test_login_through_the_gateway_sets_a_host_only_httponly_cookie():
    c = login("customer001")
    cookie = c.login_response.headers["set-cookie"].lower()
    assert "nvb_session=" in cookie and "httponly" in cookie and "samesite=lax" in cookie and "path=/" in cookie
    assert "domain=" not in cookie                       # host-only: bound to the gateway origin, no cookie-domain trick
    me = c.get("/api/auth/me").json()
    assert me["user"]["username"] == "customer001" and me["csrf_token"] == c.headers["X-CSRF-Token"]
    ident = c.get(f"{S}/me").json()
    assert ident["core_role"] == "customer" and ident["subject"].startswith("nvb-user:") and ident["extension_roles"] == []
    assert c.get(f"{L}/me").json()["subject"] == ident["subject"]      # same verified identity in both services
    assert "access-control-allow-origin" not in c.get(f"{S}/me").headers   # no CORS: same origin only


def test_wrong_password_and_unauthenticated_access():
    r = httpx.post(f"{GW}/api/auth/login", json={"username": "customer001", "password": "wrong-password"})
    assert r.status_code == 401 and "set-cookie" not in r.headers
    assert httpx.get(f"{GW}/api/auth/me").status_code == 401
    assert httpx.get(f"{GW}{S}/requests").status_code == 401
    assert httpx.get(f"{GW}{L}/applications").status_code == 401


def test_logout_ends_the_session_for_extension_apis_too():
    c = login("customer001")
    assert c.get(f"{S}/requests").status_code == 200
    assert c.post("/api/auth/logout").status_code == 200
    assert c.get(f"{S}/requests").status_code == 401
    assert c.get(f"{L}/applications").status_code == 401
    assert c.get("/api/auth/me").status_code == 401


def test_expired_session_is_rejected_by_extension_apis():
    sys.path.insert(0, str(ROOT / "scripts"))
    import local_stack

    c = login("customer004")
    assert c.get(f"{S}/requests").status_code == 200
    # Expire this user's sessions in the DISPOSABLE core database (what the idle timeout does).
    local_stack.psql("UPDATE user_sessions SET expires_at = now() - interval '1 minute' WHERE user_id = "
                     "(SELECT id FROM users WHERE username = 'customer004')", local_stack.DATABASES[0])
    assert c.get(f"{S}/requests").status_code == 401
    assert c.post(f"{S}/requests", json={"account_id": 1, "period": "2026-10"},
                  headers={"Idempotency-Key": uuid.uuid4().hex}).status_code == 401
    assert c.get("/api/auth/me").status_code == 401


# --- CSRF --------------------------------------------------------------------------------------


def test_csrf_token_is_required_and_bound_to_the_session():
    a, b = login("customer001"), login("customer002")
    accounts = a.get(f"{S}/accounts").json()["items"]
    body = {"account_id": accounts[0]["account_id"], "period": "2026-10"}
    key = {"Idempotency-Key": uuid.uuid4().hex}
    good = a.headers.pop("X-CSRF-Token")
    assert a.post(f"{S}/requests", json=body, headers=key).status_code == 403            # missing
    assert a.post(f"{S}/requests", json=body, headers={**key, "X-CSRF-Token": "x" * 43}).status_code == 403
    assert a.post(f"{S}/requests", json=body,
                  headers={**key, "X-CSRF-Token": b.headers["X-CSRF-Token"]}).status_code == 403   # other session's
    assert a.post(f"{L}/applications", json={"account_id": body["account_id"], "amount_minor": 1000,
                                             "term_months": 1, "monthly_income_minor": 0}).status_code == 403
    assert a.post("/api/auth/logout").status_code == 403                                  # the core enforces it too
    assert a.post(f"{S}/requests", json=body, headers={**key, "X-CSRF-Token": good}).status_code == 202


def test_cross_origin_state_changing_requests_are_refused_at_the_gateway():
    c = login("customer001")
    accounts = c.get(f"{S}/accounts").json()["items"]
    body = {"account_id": accounts[0]["account_id"], "period": "2026-09"}
    evil = {"Origin": "https://evil.example", "Idempotency-Key": uuid.uuid4().hex}
    assert c.post(f"{S}/requests", json=body, headers=evil).status_code == 403
    assert c.post(f"{L}/applications", json={}, headers=evil).status_code == 403
    assert httpx.post(f"{GW}/api/auth/login", json={"username": "x", "password": "y"},
                      headers={"Origin": "https://evil.example"}).status_code == 403
    same = {"Origin": GW, "Idempotency-Key": uuid.uuid4().hex}
    assert c.post(f"{S}/requests", json=body, headers=same).status_code == 202
    assert c.get(f"{S}/requests", headers={"Origin": "https://evil.example"}).status_code == 200   # reads: no CORS headers
    assert "access-control-allow-origin" not in c.get(f"{S}/requests", headers={"Origin": "https://evil.example"}).headers


# --- authorization ---------------------------------------------------------------------------------


def test_statement_request_worker_download_and_cross_customer_isolation():
    from pypdf import PdfReader

    a, b = login("customer001"), login("customer002")
    rid = request_statement(a)
    assert b.get(f"{S}/requests/{rid}").status_code == 404
    assert b.post(f"{S}/requests/{rid}/cancel").status_code == 404
    out = local_run("worker", "statements")                       # one bounded worker run (like a Job)
    assert out.returncode == 0, out.stdout[-800:] + out.stderr[-800:]
    done = wait_status(a, rid, ("completed", "failed"))
    assert done["status"] == "completed", done
    pdf = a.get(done["document_url"])
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    assert pdf.headers["cache-control"] == "no-store" and "attachment" in pdf.headers["content-disposition"]
    text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf.content)).pages)
    assert "SYNTHETIC DEMO DATA" in text
    # Unauthorized downloads: other customer, no session, staff without ownership, service token.
    assert b.get(done["document_url"]).status_code == 404
    assert httpx.get(f"{GW}{done['document_url']}").status_code == 401
    staff = login("opsadmin", "admin")
    assert staff.get(done["document_url"]).status_code == 403
    worker_token = (RUN / "tokens" / "statements-worker").read_text()
    assert httpx.get(f"{GW}{done['document_url']}", headers={"Authorization": f"Bearer {worker_token}"}).status_code == 401
    # The other customer cannot request a statement for an account that is not theirs.
    foreign = a.get(f"{S}/accounts").json()["items"][0]["account_id"]
    assert b.post(f"{S}/requests", json={"account_id": foreign, "period": "2026-10"},
                  headers={"Idempotency-Key": uuid.uuid4().hex}).status_code == 404


def test_client_supplied_identity_and_role_headers_are_ignored():
    c = login("customer003")
    admin = login("opsadmin", "admin")
    admin_subject = admin.get(f"{L}/me").json()["subject"]
    forged = {"X-User": "opsadmin", "X-Role": "admin", "X-Roles": "approver,extension_admin",
              "X-Subject": admin_subject, "X-Remote-User": "opsadmin", "X-Forwarded-User": "opsadmin",
              "X-Extension-Roles": "approver,batch_operator,extension_admin", "X-Customer-Id": "1"}
    me = c.get(f"{L}/me", headers=forged).json()
    assert me["username"] == "customer003" and me["extension_roles"] == [] and me["subject"] != admin_subject
    for path in (f"{L}/review/applications", f"{L}/admin/roles", f"{S}/batches", f"{S}/admin/roles"):
        assert c.get(path, headers=forged).status_code == 403, path
    assert admin.get(f"{S}/batches").status_code == 403           # a core admin has no extension role either


# --- routing precedence ---------------------------------------------------------------------------


def test_route_precedence_versus_the_core_spa_fallback():
    # The core itself answers unknown paths with ITS single-page app (direct to the core port):
    assert "CORE-SPA-FALLBACK-MARKER" in httpx.get(f"{CORE}/some/unknown/page").text
    c = login("customer001")
    # Through the gateway, unknown UI paths get the PORTAL, never the core's SPA ...
    page = c.get("/some/unknown/page")
    assert page.status_code == 200 and "NVB Digital Services" in page.text and "CORE-SPA" not in page.text
    assert c.get("/").text == page.text and "content-security-policy" in page.headers
    # ... and API-looking paths that are not explicitly routed return JSON 404, not an HTML page
    # and not a core answer (the core WOULD serve /api/accounts to this session).
    assert httpx.get(f"{CORE}/api/accounts", cookies=dict(c.cookies)).status_code == 200
    for path in ("/api/accounts", "/api/transactions", "/api/info", "/api/auth/register", "/api/auth/password/forgot",
                 "/api/unknown", "/x/unknown/v1/thing", "/x/", "/internal/v1/sessions/introspect",
                 "/internal/v1/discovery/statement-accounts", "/metrics", "/health/ready", "/health/live"):
        r = c.get(path)
        assert r.status_code == 404, path
        assert r.headers["content-type"].startswith("application/json") and r.json()["error"]["code"] == "not_found", path
        assert "CORE-SPA" not in r.text and "<html" not in r.text.lower()
    core_token = (RUN / "tokens" / "core-adapter").read_text()
    r = httpx.post(f"{GW}/internal/v1/sessions/introspect", headers={"Authorization": f"Bearer {core_token}"},
                   json={"session_token": c.cookies.get("nvb_session")})
    assert r.status_code == 404                                    # internal API is not reachable from the browser origin
    assert c.put("/api/auth/login", json={}).status_code in (403, 405)
    assert c.post("/index.html").status_code in (403, 405)
    assert c.get("/app.js").headers["content-type"].startswith(("application/javascript", "text/javascript"))


def test_adapter_and_health_endpoints_are_not_exposed_publicly():
    for path in ("/v1/identity/resolve", "/v1/discovery/statement-accounts"):
        r = httpx.post(f"{GW}{path}", json={"session_token": "x"})
        assert r.status_code in (403, 405) or "NVB Digital Services" in r.text   # static handler, never the adapter
        assert "active" not in r.text


# --- loans through the gateway -----------------------------------------------------------------------


def test_loan_workflow_through_the_gateway_with_explicit_roles():
    customer, reviewer, approver = login("customer002"), login("customer003"), login("opsadmin", "admin")
    rev_subject = reviewer.get(f"{L}/me").json()["subject"]
    appr_subject = approver.get(f"{L}/me").json()["subject"]
    assert local_run("grant", "loans", "reviewer", rev_subject).returncode == 0
    assert local_run("grant", "loans", "approver", appr_subject).returncode == 0
    acct = customer.get(f"{S}/accounts").json()["items"][0]["account_id"]
    d = customer.post(f"{L}/applications", json={"account_id": acct, "amount_minor": 2_400_000, "term_months": 12,
                                                 "monthly_income_minor": 800_000, "purpose": "gateway test"}).json()
    a = customer.post(f"{L}/applications/{d['id']}/submit", json={"version": d["version"]}).json()
    assert reviewer.get(f"{L}/review/applications/{a['id']}").status_code == 404        # not assigned yet
    a = approver.post(f"{L}/review/applications/{a['id']}/assign",
                      json={"reviewer_subject": rev_subject, "version": a["version"]}).json()
    assert local_run("worker", "loans").returncode == 0
    seen = reviewer.get(f"{L}/review/applications/{a['id']}").json()
    assert seen["summary"]["status"] == "completed" and "SYNTHETIC" in seen["summary"]["label"]
    reason = "Reviewed through the gateway test."
    a = reviewer.post(f"{L}/review/applications/{a['id']}/recommendation",
                      json={"recommendation": "approve", "reason": reason, "version": a["version"]}).json()
    assert reviewer.post(f"{L}/review/applications/{a['id']}/decision",
                         json={"decision": "approved", "reason": reason, "version": a["version"]}).status_code == 403
    done = approver.post(f"{L}/review/applications/{a['id']}/decision",
                         json={"decision": "approved", "reason": reason, "version": a["version"]}).json()
    assert done["status"] == "approved"
    mine = customer.get(f"{L}/applications/{a['id']}").json()
    assert mine["status"] == "approved" and "no money is posted" in mine["disbursement"]
    assert login("customer001").get(f"{L}/applications/{a['id']}").status_code == 404


# --- worker termination while processing ---------------------------------------------------------------


def test_worker_killed_mid_run_loses_nothing():
    """Hard-kill a worker process while it drains; leases expire, the relay re-queues, and a second
    bounded run completes every request exactly once."""
    c = login("customer001")
    accounts = c.get(f"{S}/accounts").json()["items"]
    rids = []
    for period in ("2026-05", "2026-06", "2026-07", "2026-08"):
        for i in range(len(accounts)):
            rids.append(request_statement(c, period, i))
    time.sleep(2.5)                                                 # relay publishes the outbox
    sys.path.insert(0, str(ROOT / "scripts"))
    import local_run as lr

    env = lr.statements_env("statements-worker")
    proc = subprocess.Popen([PY, "-m", "nvb_statements.worker", "--drain"], env=env, cwd=str(ROOT),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:                              # wait until it is really working
        if any(c.get(f"{S}/requests/{r}").json()["status"] in ("running", "completed") for r in rids):
            break
        time.sleep(0.1)
    proc.kill()                                                     # SIGKILL equivalent: no cleanup, no ack
    proc.wait(timeout=10)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        out = local_run("worker", "statements")
        assert out.returncode == 0, out.stdout[-500:] + out.stderr[-500:]
        states = [c.get(f"{S}/requests/{r}").json()["status"] for r in rids]
        if all(s == "completed" for s in states):
            break
        time.sleep(3)                                               # a killed worker's lease must expire first
    assert all(s == "completed" for s in states), states
    for r in rids[:3]:
        assert c.get(f"{S}/requests/{r}/document").content.startswith(b"%PDF")
