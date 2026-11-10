"""statements-api: authentication, CSRF, ownership, idempotency and download authorization."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from nvb_statements.models import OutboxMessage, StatementEvent, StatementRequest
from nvb_statements.relay import publish_pending
from tests.conftest import A_SUBJECT, browser

URL = "/x/statements/v1/requests"


def create(c, account_id=11, period="2026-09", key=None):
    return c.post(URL, json={"account_id": account_id, "period": period},
                  headers={"Idempotency-Key": key or uuid.uuid4().hex})


def run_pipeline(sm, broker, worker):
    publish_pending(sm, broker, 100)
    while (d := broker.get(worker.s.queue_name, 0)) is not None:
        worker.handle(d)


# --- creation ----------------------------------------------------------------------------


def test_create_returns_202_with_status_url_and_writes_outbox_atomically(app, fake, sm):
    r = create(browser(app, fake))
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "queued" and body["status_url"].endswith(body["id"])
    assert r.headers["Location"] == body["status_url"]
    assert body["period"] == {"label": "2026-09", "timezone": "Africa/Cairo", "start_utc": "2026-08-31T21:00:00+00:00",
                              "end_utc": "2026-09-30T21:00:00+00:00", "boundary": "[start,end)"}
    with sm() as db:
        assert db.scalar(select(func.count()).select_from(OutboxMessage)) == 1
        req = db.get(StatementRequest, body["id"])
        assert req.owner_subject == A_SUBJECT and req.purpose == "customer_request"
        assert req.data_scope == "statement:single_account"
        assert db.scalar(select(StatementEvent.event_type).where(StatementEvent.request_id == req.id)) == "requested"


def test_same_idempotency_key_replays_without_duplicate(app, fake, sm):
    c = browser(app, fake)
    r1, r2 = create(c, key="key-12345678"), create(c, key="key-12345678")
    assert r1.json()["id"] == r2.json()["id"] and r2.headers["Idempotent-Replayed"] == "true"
    with sm() as db:
        assert db.scalar(select(func.count()).select_from(StatementRequest)) == 1
        assert db.scalar(select(func.count()).select_from(OutboxMessage)) == 1


def test_same_key_different_payload_is_rejected(app, fake):
    c = browser(app, fake)
    assert create(c, period="2026-09", key="key-abcdefgh").status_code == 202
    r = create(c, period="2026-08", key="key-abcdefgh")
    assert r.status_code == 409 and r.json()["error"]["code"] == "idempotency_conflict"


def test_idempotency_keys_are_scoped_per_customer(app, fake):
    assert create(browser(app, fake, "a"), key="shared-key-1").status_code == 202
    assert create(browser(app, fake, "b"), account_id=21, key="shared-key-1").status_code == 202


@pytest.mark.parametrize("key", [None, "short", "has space in it", "x" * 65])
def test_idempotency_key_required_and_validated(app, fake, key):
    c = browser(app, fake)
    headers = {} if key is None else {"Idempotency-Key": key}
    assert c.post(URL, json={"account_id": 11, "period": "2026-09"}, headers=headers).status_code == 422


@pytest.mark.parametrize("period,code", [("2026-11", "period_not_closed"), ("2024-01", "period_too_old"),
                                         ("2026-13", None), ("2026-9", None)])
def test_period_validation(app, fake, period, code):
    r = create(browser(app, fake), period=period)
    assert r.status_code == 422
    if code:
        assert r.json()["error"]["code"] == code


def test_cannot_request_statement_for_someone_elses_account(app, fake):
    r = create(browser(app, fake, "a"), account_id=21)
    assert r.status_code == 404 and r.json()["error"]["code"] == "account_not_found"


# --- authentication / CSRF / forged headers ------------------------------------------------


def test_no_session_is_401(app):
    c = TestClient(app)
    assert c.get(URL).status_code == 401
    assert c.post(URL, json={"account_id": 11, "period": "2026-09"},
                  headers={"Idempotency-Key": uuid.uuid4().hex, "X-CSRF-Token": "x"}).status_code == 401


def test_unknown_or_revoked_session_is_401(app, fake):
    c = browser(app, fake)
    fake.sessions.pop(fake.a_token)  # logout / expiry / suspension at the core
    assert c.get(URL).status_code == 401


@pytest.mark.parametrize("csrf", [None, "forged-token"])
def test_mutations_require_valid_csrf(app, fake, csrf):
    c = browser(app, fake)
    if csrf is None:
        del c.headers["X-CSRF-Token"]
    else:
        c.headers["X-CSRF-Token"] = csrf
    r = create(c)
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_invalid"


def test_csrf_of_another_session_is_rejected(app, fake):
    c = browser(app, fake, "a")
    c.headers["X-CSRF-Token"] = fake.b_csrf
    assert create(c).status_code == 403


def test_forged_identity_headers_are_ignored(app, fake, sm):
    c = browser(app, fake, "a")
    c.headers.update({"X-User": "customer002", "X-Subject": "nvb-user:2:1700000001", "X-Role": "admin"})
    body = create(c).json()
    with sm() as db:
        assert db.get(StatementRequest, body["id"]).owner_subject == A_SUBJECT


def test_non_customer_roles_rejected(app, fake):
    assert browser(app, fake, "admin").get(URL).status_code == 403


def test_identity_outage_is_503_not_success(app, fake):
    c = browser(app, fake)
    fake.unavailable = True
    r = create(c)
    assert r.status_code == 503 and r.json()["error"]["code"] == "identity_unavailable"


# --- isolation of read/list/download --------------------------------------------------


def test_customer_b_cannot_read_list_cancel_or_download_a(app, fake, sm, broker, worker):
    a, b = browser(app, fake, "a"), browser(app, fake, "b")
    rid = create(a).json()["id"]
    run_pipeline(sm, broker, worker)
    assert a.get(f"{URL}/{rid}").json()["status"] == "completed"
    assert b.get(f"{URL}/{rid}").status_code == 404
    assert b.get(f"{URL}/{rid}/document").status_code == 404
    assert b.post(f"{URL}/{rid}/cancel").status_code == 404
    assert all(i["id"] != rid for i in b.get(URL).json()["items"])
    assert [i["id"] for i in a.get(URL).json()["items"]] == [rid]


def test_download_requires_current_account_ownership(app, fake, sm, broker, worker):
    a = browser(app, fake, "a")
    rid = create(a).json()["id"]
    run_pipeline(sm, broker, worker)
    assert a.get(f"{URL}/{rid}/document").status_code == 200
    # Account moved away from the customer at the core -> no longer downloadable.
    fake.sessions[fake.a_token]["accounts"] = [x for x in fake.sessions[fake.a_token]["accounts"]
                                               if x["account_id"] != 11]
    assert a.get(f"{URL}/{rid}/document").status_code == 404


def test_download_after_logout_fails(app, fake, sm, broker, worker):
    a = browser(app, fake, "a")
    rid = create(a).json()["id"]
    run_pipeline(sm, broker, worker)
    fake.sessions.pop(fake.a_token)
    assert a.get(f"{URL}/{rid}/document").status_code == 401


def test_download_is_pdf_with_integrity_check(app, fake, sm, broker, worker, store):
    a = browser(app, fake, "a")
    rid = create(a).json()["id"]
    assert a.get(f"{URL}/{rid}/document").status_code == 409  # not ready yet
    run_pipeline(sm, broker, worker)
    r = a.get(f"{URL}/{rid}/document")
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    assert r.headers["content-type"] == "application/pdf"
    assert "attachment" in r.headers["content-disposition"] and r.headers["cache-control"] == "no-store"
    with sm() as db:
        key = db.get(StatementRequest, rid).result_key
    store.objects[key] = store.objects[key] + b"tampered"
    assert a.get(f"{URL}/{rid}/document").status_code == 500


def test_cancel_only_queued(app, fake, sm, broker, worker):
    a = browser(app, fake, "a")
    rid = create(a).json()["id"]
    assert a.post(f"{URL}/{rid}/cancel").json()["status"] == "cancelled"
    assert a.post(f"{URL}/{rid}/cancel").status_code == 409
    run_pipeline(sm, broker, worker)  # queued message for a cancelled request is acked, not processed
    assert a.get(f"{URL}/{rid}").json()["status"] == "cancelled"
    assert "manifest" not in fake.calls


def test_health(app):
    c = TestClient(app)
    assert c.get("/health/live").status_code == 200
    assert c.get("/health/ready").json()["status"] == "ready"
