"""Batch orchestration, extension roles, queue separation and bounded worker runs."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from nvb_platform import roles as roles_lib
from nvb_platform.adapter_client import AdapterCallError
from nvb_statements import batch as batch_ops
from nvb_statements.config import Settings
from nvb_statements.models import (
    KNOWN_ROLES,
    BatchItem,
    OutboxMessage,
    RoleAssignment,
    RoleEvent,
    StatementBatch,
    StatementRequest,
    utcnow,
)
from nvb_statements.relay import publish_pending, reconcile
from nvb_statements.service import ServiceError
from nvb_statements.worker import LeaseKeeper, Worker
from tests.conftest import A_SUBJECT, B_SUBJECT, browser
from tests.fakes import account

BASE = "/x/statements/v1"
ADMIN_SUBJECT = "nvb-user:3:1700000002"


def grant(sm, subject, role):
    with sm() as db:
        roles_lib.set_role(db, RoleAssignment, RoleEvent, subject=subject, role=role, active=True,
                           actor="test", known_roles=KNOWN_ROLES)
        db.commit()


def key():
    return {"Idempotency-Key": uuid.uuid4().hex}


def drain(sm, broker, worker):
    publish_pending(sm, broker, 1000)
    out = []
    for q in worker.queues:
        while (d := broker.get(q, 0)) is not None:
            out.append(worker.handle(d))
    return out


# --- roles -------------------------------------------------------------------------------


def test_core_admin_role_grants_no_extension_capability(app, fake, sm):
    admin = browser(app, fake, "admin")
    me = admin.get(f"{BASE}/me").json()
    assert me["core_role"] == "admin" and me["extension_roles"] == [] and me["subject"] == ADMIN_SUBJECT
    assert admin.post(f"{BASE}/batches", json={"period": "2026-09"}, headers=key()).status_code == 403
    assert admin.get(f"{BASE}/batches").status_code == 403
    assert admin.get(f"{BASE}/admin/roles").status_code == 403


def test_customer_cannot_use_staff_endpoints_and_staff_cannot_read_customer_data(app, fake, sm):
    assert browser(app, fake, "a").get(f"{BASE}/batches").status_code == 403
    grant(sm, ADMIN_SUBJECT, "batch_operator")
    admin = browser(app, fake, "admin")
    assert admin.get(f"{BASE}/batches").status_code == 200
    assert admin.get(f"{BASE}/requests").status_code == 403            # customer-only
    assert admin.get(f"{BASE}/accounts").status_code == 403


def test_role_admin_grant_revoke_is_audited_and_effective(app, fake, sm):
    grant(sm, ADMIN_SUBJECT, "extension_admin")
    admin = browser(app, fake, "admin")
    customer = browser(app, fake, "a")
    assert customer.get(f"{BASE}/batches").status_code == 403
    r = admin.post(f"{BASE}/admin/roles/grant", json={"subject": A_SUBJECT, "role": "batch_operator",
                                                      "reason": "demo operator"})
    assert r.status_code == 200 and r.json()["changed"] is True
    assert customer.get(f"{BASE}/batches").status_code == 200
    assert admin.post(f"{BASE}/admin/roles/grant", json={"subject": A_SUBJECT, "role": "batch_operator"}
                      ).json()["changed"] is False
    assert admin.post(f"{BASE}/admin/roles/revoke", json={"subject": A_SUBJECT, "role": "batch_operator"}
                      ).json()["changed"] is True
    assert customer.get(f"{BASE}/batches").status_code == 403
    with sm() as db:
        events = db.scalars(select(RoleEvent).where(RoleEvent.subject == A_SUBJECT).order_by(RoleEvent.id)).all()
    assert [(e.action, e.actor) for e in events] == [("grant", ADMIN_SUBJECT), ("revoke", ADMIN_SUBJECT)]


def test_role_admin_validation(app, fake, sm):
    grant(sm, ADMIN_SUBJECT, "extension_admin")
    admin = browser(app, fake, "admin")
    assert admin.post(f"{BASE}/admin/roles/grant", json={"subject": A_SUBJECT, "role": "approver"}
                      ).json()["error"]["code"] == "unknown_role"          # not a role of this service
    assert admin.post(f"{BASE}/admin/roles/grant", json={"subject": "customer001", "role": "batch_operator"}
                      ).status_code == 422
    assert admin.post(f"{BASE}/admin/roles/revoke", json={"subject": ADMIN_SUBJECT, "role": "extension_admin"}
                      ).json()["error"]["code"] == "self_revoke_forbidden"
    # CSRF is required for role changes
    admin.headers.pop("X-CSRF-Token")
    assert admin.post(f"{BASE}/admin/roles/grant", json={"subject": A_SUBJECT, "role": "batch_operator"}
                      ).status_code == 403


def test_accounts_endpoint_lists_only_own_accounts(app, fake):
    items = browser(app, fake, "a").get(f"{BASE}/accounts").json()["items"]
    assert [a["account_id"] for a in items] == [11, 12]
    assert [a["account_id"] for a in browser(app, fake, "b").get(f"{BASE}/accounts").json()["items"]] == [21]


# --- batch creation --------------------------------------------------------------------------


@pytest.fixture
def operator(app, fake, sm):
    grant(sm, ADMIN_SUBJECT, "batch_operator")
    return browser(app, fake, "admin")


def test_batch_freezes_manifest_then_releases_members_on_the_batch_queue(app, fake, sm, broker, worker, operator,
                                                                           settings):
    r = operator.post(f"{BASE}/batches", json={"period": "2026-09"}, headers=key())
    assert r.status_code == 202, r.text
    b = r.json()
    assert b["status"] == "running" and b["trigger"] == "manual" and b["created_by"] == ADMIN_SUBJECT
    assert b["progress"] == {"queued": 3, "running": 0, "completed": 0, "failed": 0, "cancelled": 0, "total": 3,
                             "pending_release": 0}
    with sm() as db:
        items = db.scalars(select(BatchItem).where(BatchItem.batch_id == b["id"]).order_by(BatchItem.account_id)).all()
        assert [(i.account_id, i.owner_subject) for i in items] == [(11, A_SUBJECT), (12, A_SUBJECT), (21, B_SUBJECT)]
        assert all(i.request_id for i in items)
        reqs = db.scalars(select(StatementRequest).where(StatementRequest.batch_id == b["id"])).all()
        assert {q.queue for q in reqs} == {settings.batch_queue_name} and {q.purpose for q in reqs} == {"batch"}
        assert {q.requested_by_subject for q in reqs} == {ADMIN_SUBJECT}
        assert {m.queue for m in db.scalars(select(OutboxMessage)).all()} == {settings.batch_queue_name}
    assert drain(sm, broker, worker) == ["completed"] * 3
    done = operator.get(f"{BASE}/batches/{b['id']}").json()
    assert done["status"] == "completed" and done["progress"]["completed"] == 3 and done["failures"] == []
    # The owner can see and download the batch-generated statement; others cannot.
    mine = browser(app, fake, "a").get(f"{BASE}/requests").json()["items"]
    assert len(mine) == 2 and {m["purpose"] for m in mine} == {"batch"}
    assert browser(app, fake, "a").get(mine[0]["document_url"]).status_code == 200
    assert browser(app, fake, "b").get(mine[0]["document_url"]).status_code == 404


def test_batch_create_is_idempotent(app, fake, sm, operator):
    k = key()
    first = operator.post(f"{BASE}/batches", json={"period": "2026-09"}, headers=k)
    again = operator.post(f"{BASE}/batches", json={"period": "2026-09"}, headers=k)
    assert again.json()["id"] == first.json()["id"] and again.headers["Idempotent-Replayed"] == "true"
    assert operator.post(f"{BASE}/batches", json={"period": "2026-08"}, headers=k).status_code == 409
    with sm() as db:
        assert db.scalar(select(func.count()).select_from(StatementBatch)) == 1
        assert db.scalar(select(func.count()).select_from(StatementRequest)) == 3


def test_batch_rejects_open_or_invalid_period(operator):
    r = operator.post(f"{BASE}/batches", json={"period": "2099-01"}, headers=key())
    assert r.status_code == 422 and r.json()["error"]["code"] == "period_not_closed"
    assert operator.post(f"{BASE}/batches", json={"period": "2026-13"}, headers=key()).status_code == 422
    assert operator.post(f"{BASE}/batches", json={"period": "2026-09"}).status_code == 422   # no idempotency key


def test_interrupted_freeze_enqueues_nothing_and_resumes_from_the_cursor(app, fake, sm, operator, settings):
    for n in range(30, 36):
        fake.add_customer(f"nvb-user:{n}:1700000000", [account(n * 10)])
    settings.batch_discovery_page_size = 2
    fake.discovery_fail_after = 2            # two pages succeed, the third call fails
    k = key()
    r = operator.post(f"{BASE}/batches", json={"period": "2026-09"}, headers=k)
    b = r.json()
    assert r.status_code == 202 and b["status"] == "freezing" and b["progress"]["total"] == 4
    with sm() as db:
        assert db.scalar(select(func.count()).select_from(StatementRequest)) == 0     # nothing released yet
        assert db.scalar(select(func.count()).select_from(OutboxMessage)) == 0
    resumed = operator.post(f"{BASE}/batches", json={"period": "2026-09"}, headers=k).json()   # same key resumes
    assert resumed["id"] == b["id"] and resumed["status"] == "running" and resumed["progress"]["total"] == 9
    with sm() as db:
        assert db.scalar(select(func.count()).select_from(BatchItem)) == 9
        assert db.scalar(select(func.count()).select_from(StatementRequest)) == 9
    assert fake.calls.count("discovery") == 3 + 3     # 2 ok + 1 failed, then pages 3..5 only (not from the start)


def test_release_is_chunked_and_never_duplicates_members(sm, fake, settings):
    for n in range(30, 37):
        fake.add_customer(f"nvb-user:{n}:1700000000", [account(n * 10)])
    settings.batch_release_chunk = 3
    with sm() as db:
        b, created = batch_ops.create_batch(db, settings, period_label="2026-09", created_by="system:schedule",
                                            trigger="schedule", idempotency_key="schedule-2026-09")
    assert created
    batch_ops.advance_batch(sm, fake, settings, b.id)
    batch_ops.advance_batch(sm, fake, settings, b.id)      # calling again is a no-op
    with sm() as db:
        assert db.scalar(select(func.count()).select_from(StatementRequest)) == 10
        assert db.scalar(select(func.count(func.distinct(StatementRequest.account_id)))) == 10
        assert db.get(StatementBatch, b.id).status == "running"


def test_scheduled_and_manual_batches_use_the_same_operation(sm, fake, settings, operator):
    with sm() as db:
        sched, _ = batch_ops.create_batch(db, settings, period_label="2026-09", created_by="system:schedule",
                                          trigger="schedule", idempotency_key="schedule-2026-09")
    batch_ops.advance_batch(sm, fake, settings, sched.id)
    manual = operator.post(f"{BASE}/batches", json={"period": "2026-09"}, headers=key()).json()
    with sm() as db:
        a = db.scalars(select(BatchItem.account_id).where(BatchItem.batch_id == sched.id)).all()
        b = db.scalars(select(BatchItem.account_id).where(BatchItem.batch_id == manual["id"])).all()
        assert sorted(a) == sorted(b) == [11, 12, 21]
        again, created = batch_ops.create_batch(db, settings, period_label="2026-09", created_by="system:schedule",
                                                trigger="schedule", idempotency_key="schedule-2026-09")
        assert not created and again.id == sched.id          # one scheduled batch per period


def test_batch_over_the_account_cap_fails_without_enqueuing(sm, fake, settings):
    settings.batch_max_accounts = 2
    with sm() as db:
        b, _ = batch_ops.create_batch(db, settings, period_label="2026-09", created_by="t", trigger="schedule",
                                      idempotency_key="cap-test-1")
    out = batch_ops.advance_batch(sm, fake, settings, b.id)
    assert out.status == "failed" and out.error_code == "too_many_accounts"
    with sm() as db:
        assert db.scalar(select(func.count()).select_from(StatementRequest)) == 0


def test_load_test_accounts_are_excluded_unless_requested(sm, fake, settings):
    fake.load_test_accounts = {21}
    with sm() as db:
        b, _ = batch_ops.create_batch(db, settings, period_label="2026-09", created_by="t", trigger="schedule",
                                      idempotency_key="lt-test-1")
    batch_ops.advance_batch(sm, fake, settings, b.id)
    with sm() as db:
        assert sorted(db.scalars(select(BatchItem.account_id).where(BatchItem.batch_id == b.id))) == [11, 12]


# --- failures, retry, cancel -------------------------------------------------------------------


def test_failed_members_are_reported_and_retry_is_bounded(app, fake, sm, broker, worker, operator, settings):
    settings.batch_max_retry_rounds = 1
    b = operator.post(f"{BASE}/batches", json={"period": "2026-09"}, headers=key()).json()
    fake.owners[21] = "nvb-user:99:1"          # ownership changed after the freeze -> authorization revoked
    assert sorted(drain(sm, broker, worker)) == ["completed", "completed", "failed"]
    got = operator.get(f"{BASE}/batches/{b['id']}").json()
    assert got["status"] == "completed_with_failures"
    assert [f["error_code"] for f in got["failures"]] == ["authorization_revoked"]
    assert "•••• 2121" == got["failures"][0]["account_masked"]

    fake.owners[21] = B_SUBJECT                 # condition fixed; operator retries the failed members only
    r = operator.post(f"{BASE}/batches/{b['id']}/retry-failed").json()
    assert r["requeued"] == 1 and r["status"] == "running" and r["retry_rounds"] == 1
    assert drain(sm, broker, worker) == ["completed"]
    assert operator.get(f"{BASE}/batches/{b['id']}").json()["status"] == "completed"
    r = operator.post(f"{BASE}/batches/{b['id']}/retry-failed")
    assert r.status_code == 409                 # finished cleanly: nothing to retry


def test_retry_rounds_limit(app, fake, sm, broker, worker, operator, settings):
    settings.batch_max_retry_rounds = 1
    b = operator.post(f"{BASE}/batches", json={"period": "2026-09"}, headers=key()).json()
    fake.owners[21] = "nvb-user:99:1"
    drain(sm, broker, worker)
    assert operator.post(f"{BASE}/batches/{b['id']}/retry-failed").json()["requeued"] == 1
    drain(sm, broker, worker)
    r = operator.post(f"{BASE}/batches/{b['id']}/retry-failed")
    assert r.status_code == 409 and r.json()["error"]["code"] == "retry_limit_reached"


def test_cancel_batch_stops_queued_members_and_keeps_results(app, fake, sm, broker, worker, operator):
    b = operator.post(f"{BASE}/batches", json={"period": "2026-09"}, headers=key()).json()
    publish_pending(sm, broker, 1000)
    worker.handle(broker.get(worker.queues[1], 0))                 # one member completes
    r = operator.post(f"{BASE}/batches/{b['id']}/cancel").json()
    assert r["status"] == "cancelled" and r["cancelled_members"] == 2
    assert drain(sm, broker, worker) == ["already_cancelled", "already_cancelled"]
    assert r["progress"]["completed"] == 1
    assert operator.post(f"{BASE}/batches/{b['id']}/cancel").status_code == 409


def test_relay_finalizes_batches_without_api_polling(app, fake, sm, broker, worker, operator):
    b = operator.post(f"{BASE}/batches", json={"period": "2026-09"}, headers=key()).json()
    drain(sm, broker, worker)
    assert batch_ops.finalize_running(sm) == 1
    with sm() as db:
        assert db.get(StatementBatch, b["id"]).status == "completed"


# --- queues and bounded worker runs --------------------------------------------------------------


def test_worker_prefers_the_interactive_queue_over_the_batch_queue(app, fake, sm, broker, worker, operator):
    operator.post(f"{BASE}/batches", json={"period": "2026-09"}, headers=key())
    rid = browser(app, fake, "a").post(f"{BASE}/requests", json={"account_id": 11, "period": "2026-08"},
                                       headers=key()).json()["id"]
    publish_pending(sm, broker, 1000)
    assert broker.depth(worker.queues[0]) == 1 and broker.depth(worker.queues[1]) == 3
    assert worker.run(drain=True, max_messages=1) == 0
    with sm() as db:
        assert db.get(StatementRequest, rid).status == "completed"            # the customer request went first
        assert db.scalar(select(func.count()).select_from(StatementRequest)
                         .where(StatementRequest.status == "completed")) == 1


def test_bounded_run_stops_after_max_messages_and_leaves_the_rest_queued(app, fake, sm, broker, worker, operator):
    operator.post(f"{BASE}/batches", json={"period": "2026-09"}, headers=key())
    publish_pending(sm, broker, 1000)
    assert worker.run(drain=True, max_messages=2) == 0
    assert broker.depth(worker.queues[1]) == 1 and not broker.unacked
    assert worker.run(drain=True, max_messages=50) == 0                      # next Job finishes and exits on idle
    assert broker.depth(worker.queues[1]) == 0


def test_runtime_bound_and_stop_request(app, fake, sm, broker, worker, operator):
    operator.post(f"{BASE}/batches", json={"period": "2026-09"}, headers=key())
    publish_pending(sm, broker, 1000)
    import time

    handle = worker.handle

    def slow_handle(d):
        time.sleep(0.2)
        return handle(d)

    worker.handle = slow_handle
    assert worker.run(drain=True, max_runtime_seconds=0.1) == 0             # bound reached after one message
    assert broker.depth(worker.queues[1]) == 2 and not broker.unacked
    worker.stopping = True                                                   # SIGTERM before the next message
    assert worker.run(drain=True) == 0
    assert broker.depth(worker.queues[1]) == 2 and not broker.unacked


def test_reconciler_requeues_on_the_requests_own_queue(app, fake, sm, broker, worker, operator, settings):
    b = operator.post(f"{BASE}/batches", json={"period": "2026-09"}, headers=key()).json()
    publish_pending(sm, broker, 1000)
    with sm() as db:                                                         # simulate a worker that died mid-task
        db.execute(update(StatementRequest).where(StatementRequest.batch_id == b["id"])
                   .values(status="running", lease_owner="dead", lease_expires_at=utcnow() - timedelta(seconds=1)))
        db.commit()
    assert reconcile(sm, settings.queue_name) == 3
    with sm() as db:
        fresh = db.scalars(select(OutboxMessage).where(OutboxMessage.published_at.is_(None))).all()
        assert len(fresh) == 3 and {m.queue for m in fresh} == {settings.batch_queue_name}


# --- lease heartbeat ---------------------------------------------------------------------------


def test_lease_is_renewed_while_owned_and_reported_lost_otherwise(app, fake, sm, broker, worker):
    rid = browser(app, fake, "a").post(f"{BASE}/requests", json={"account_id": 11, "period": "2026-09"},
                                       headers=key()).json()["id"]
    with sm() as db:
        db.execute(update(StatementRequest).where(StatementRequest.id == rid)
                   .values(status="running", lease_owner="w1", lease_expires_at=utcnow() + timedelta(seconds=1)))
        db.commit()
    keeper = LeaseKeeper(sm, rid, "w1", lease_seconds=120)
    assert keeper.renew() is True
    with sm() as db:
        left = db.get(StatementRequest, rid).lease_expires_at
        assert left.replace(tzinfo=utcnow().tzinfo) > utcnow() + timedelta(seconds=60)
    with sm() as db:
        db.execute(update(StatementRequest).where(StatementRequest.id == rid).values(lease_owner="other"))
        db.commit()
    assert keeper.renew() is False


def test_heartbeat_thread_extends_the_lease_during_a_long_task(app, fake, sm, broker, store, settings):
    """A task longer than the lease must not be stolen: the heartbeat keeps the lease alive."""
    import time

    settings.lease_seconds = 10                       # minimum allowed; heartbeat interval = lease/3
    rid = browser(app, fake, "a").post(f"{BASE}/requests", json={"account_id": 11, "period": "2026-09"},
                                       headers=key()).json()["id"]
    w = Worker(settings, sm, broker, store, fake, worker_id="slow")
    original = fake.entries
    seen = {}

    def slow_entries(*a, **kw):
        with sm() as db:
            seen["before"] = db.get(StatementRequest, rid).lease_expires_at
        time.sleep(4.5)                               # > lease/3 -> at least one heartbeat fires
        with sm() as db:
            seen["after"] = db.get(StatementRequest, rid).lease_expires_at
        return original(*a, **kw)

    fake.entries = slow_entries
    assert drain(sm, broker, w) == ["completed"]
    assert seen["after"] > seen["before"]


# --- digest verification (contract statement-v2) --------------------------------------------------


def test_in_place_edit_with_identical_count_and_totals_is_rejected(app, fake, sm, broker, worker, settings):
    """Count/total checks alone would accept this dataset; the per-entry digest does not."""
    rid = browser(app, fake, "a").post(f"{BASE}/requests", json={"account_id": 11, "period": "2026-09"},
                                       headers=key()).json()["id"]
    settings.entries_page_size = 2
    worker.s.entries_page_size = 2
    fake.tamper_after_first_page = True
    assert drain(sm, broker, worker) == ["retry_scheduled"]
    with sm() as db:
        r = db.get(StatementRequest, rid)
        assert r.status == "queued" and r.error_code == "dataset_incomplete" and r.result_key is None


def test_unsupported_dataset_version_is_not_rendered(app, fake, sm, broker, worker):
    rid = browser(app, fake, "a").post(f"{BASE}/requests", json={"account_id": 11, "period": "2026-09"},
                                       headers=key()).json()["id"]
    original = fake.manifest
    fake.manifest = lambda *a, **kw: {**original(*a, **kw), "dataset_version": "statement-v1"}
    assert drain(sm, broker, worker) == ["retry_scheduled"]
    with sm() as db:
        assert db.get(StatementRequest, rid).result_key is None


# --- configuration: no implicit SQLite, explicit failures --------------------------------------------


def test_no_database_setting_is_an_error_not_a_sqlite_fallback(monkeypatch):
    from nvb_statements.config import ConfigError

    with pytest.raises(ConfigError, match="STATEMENTS_DATABASE_URL is not set"):
        Settings().resolved_database_url()
    with pytest.raises(ConfigError, match="SQLite"):
        Settings(database_url="sqlite:///./x.db").resolved_database_url()
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    with pytest.raises(ConfigError, match="inside Kubernetes"):
        Settings(database_url="sqlite:///./x.db", allow_sqlite=True).resolved_database_url()


def test_postgres_url_must_not_embed_a_password_and_needs_the_file(tmp_path):
    from nvb_statements.config import ConfigError

    with pytest.raises(ConfigError, match="must not embed a password"):
        Settings(database_url="postgresql+psycopg://u:secret@db/x").resolved_database_url()
    with pytest.raises(ConfigError, match="password file"):
        Settings(database_url="postgresql+psycopg://u@db/x").resolved_database_url()
    pw = tmp_path / "pw"
    pw.write_text("s3cr3t-value\n", encoding="utf-8")
    url = Settings(database_url="postgresql+psycopg://u@db/x", database_password_file=str(pw)).resolved_database_url()
    assert url.password == "s3cr3t-value" and "s3cr3t-value" not in str(url)   # rendered URL hides the password


def test_validate_runtime_lists_missing_settings_without_values(tmp_path):
    problems = Settings().validate_runtime("worker")
    text = " ".join(problems)
    for name in ("STATEMENTS_DATABASE_URL", "STATEMENTS_ADAPTER_TOKEN_FILE", "STATEMENTS_BROKER_URL",
                 "STATEMENTS_S3_ENDPOINT_URL"):
        assert name in text
    tok = tmp_path / "t"
    tok.write_text("x" * 40, encoding="utf-8")
    s = Settings(database_url="postgresql+psycopg://u@db/x", database_password_file=str(tok),
                 adapter_base_url="http://core-adapter:8080", adapter_token_file=str(tok),
                 broker_url="amqp://rabbit:5672/nvb", broker_username_file=str(tok), broker_password_file=str(tok),
                 s3_endpoint_url="http://s3:9000", s3_access_key_file=str(tok), s3_secret_key_file=str(tok))
    text = " ".join(s.validate_runtime("worker"))
    assert "http:// without STATEMENTS_ALLOW_INSECURE_HTTP" in text
    assert "amqp:// without STATEMENTS_ALLOW_PLAINTEXT_DEPENDENCIES" in text
    assert "x" * 40 not in text
    ok = s.model_copy(update={"adapter_base_url": "https://core-adapter:8443", "broker_url": "amqps://rabbit:5671/nvb",
                              "s3_endpoint_url": "https://s3.example"})
    assert ok.validate_runtime("worker") == [] and ok.validate_runtime("api") == []
    embedded = s.model_copy(update={"broker_url": "amqps://user:pw@rabbit:5671/nvb"})
    assert "must not embed credentials" in " ".join(embedded.validate_runtime("relay"))


def test_service_error_shape_for_unknown_batch(operator):
    r = operator.get(f"{BASE}/batches/{uuid.uuid4()}")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    assert isinstance(ServiceError(404, "x", "y"), Exception) and AdapterCallError
