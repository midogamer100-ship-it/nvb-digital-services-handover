"""Read-only statement operations view for the Operations Admin."""

from __future__ import annotations

import uuid

from nvb_statements.models import StatementRequest
from tests.conftest import A_SUBJECT, browser
from tests.test_batch_roles import ADMIN_SUBJECT, grant

BASE = "/x/statements/v1"


def _request(client, account_id=11, period="2026-09"):
    r = client.post(f"{BASE}/requests", json={"account_id": account_id, "period": period},
                    headers={"Idempotency-Key": uuid.uuid4().hex})
    assert r.status_code in (200, 202), r.text
    return r.json()


def test_ops_views_require_core_admin_and_explicit_role(app, fake, sm):
    grant(sm, A_SUBJECT, "operations_admin")                 # a customer holding the row gets nothing
    for who in ("a", "admin"):                               # admin: core admin WITHOUT the assignment
        c = browser(app, fake, who)
        assert c.get(f"{BASE}/ops/summary").status_code == 403
        assert c.get(f"{BASE}/ops/requests").status_code == 403
    me = browser(app, fake, "a").get(f"{BASE}/me").json()
    assert me["extension_roles"] == ["operations_admin"] and me["effective_roles"] == []
    assert me["capabilities"]["statement_operations"] is False


def test_ops_summary_and_requests_leak_no_customer_documents(app, fake, sm):
    customer = browser(app, fake, "a")
    created = _request(customer)
    with sm() as db:                                         # one failed request with an internal message
        failed = db.get(StatementRequest, created["id"])
        failed.status, failed.error_code, failed.error_message = "failed", "core_unavailable", "internal host x:1"
        db.commit()
    _request(customer, account_id=12)
    grant(sm, ADMIN_SUBJECT, "operations_admin")
    admin = browser(app, fake, "admin")
    me = admin.get(f"{BASE}/me").json()
    assert me["capabilities"]["statement_operations"] is True and me["capabilities"]["batch_operations"] is False
    summary = admin.get(f"{BASE}/ops/summary").json()
    assert summary["by_status"]["failed"] == 1 and summary["by_status"]["queued"] == 1
    assert summary["by_purpose"] == {"customer_request": 2, "batch": 0}
    assert summary["recent_failures"][0]["error_code"] == "core_unavailable"
    listing = admin.get(f"{BASE}/ops/requests", params={"status": "failed"}).json()["items"]
    assert [x["id"] for x in listing] == [created["id"]]
    text = str(summary) + str(admin.get(f"{BASE}/ops/requests").json())
    for leak in (A_SUBJECT, "internal host", "document", "result_key", "owner"):
        assert leak not in text
    assert admin.get(f"{BASE}/requests/{created['id']}/document").status_code == 403   # customers only
    assert admin.get(f"{BASE}/ops/requests", params={"limit": 1000}).status_code == 422
    assert admin.post(f"{BASE}/batches", json={"period": "2026-09"},
                      headers={"Idempotency-Key": uuid.uuid4().hex}).status_code == 403   # batches stay separate
    assert [x["id"] for x in customer.get(f"{BASE}/requests").json()["items"]]            # customer flow intact
