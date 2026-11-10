"""Backend end-to-end on real dependencies.

browser session -> statements-api (PostgreSQL) -> outbox -> relay -> RabbitMQ -> worker ->
Core Adapter -> patched core on PostgreSQL (REPEATABLE READ export, digest verified) ->
PDF -> S3-compatible storage -> authorised download.

The core and the adapter run in-process (ASGI) - the HTTP hop between them is exercised by the
gateway/browser suite. PostgreSQL, RabbitMQ and the object store are the real, disposable ones.
"""

from __future__ import annotations

import io
import uuid

import pytest
from sqlalchemy import select

URL = "/x/statements/v1"


@pytest.fixture
def chain(core_stack, stm_settings, stm_sm):
    from fastapi.testclient import TestClient

    from nvb_platform.adapter_client import AdapterClient
    from nvb_platform.broker import RabbitBroker
    from nvb_statements.api import create_app
    from nvb_statements.storage import S3Store
    from nvb_statements.worker import Worker

    def adapter(client_name):
        return AdapterClient(stm_settings.model_copy(update={"adapter_token_file": core_stack["token_files"][client_name]}),
                             transport=core_stack["adapter_transport"])

    store = S3Store(stm_settings)
    api = create_app(stm_settings, adapter=adapter("statements-api"), store=store, sessionmaker=stm_sm)
    relay_broker = RabbitBroker(stm_settings, declare=(stm_settings.queue_name, stm_settings.batch_queue_name))
    made = []

    def worker(worker_id="w1", crash_at=None):
        b = RabbitBroker(stm_settings, verify=(stm_settings.queue_name, stm_settings.batch_queue_name))
        made.append(b)
        return Worker(stm_settings, stm_sm, b, store, adapter("statements-worker"), worker_id=worker_id,
                      crash_at=crash_at)

    def portal(username):
        core_client, session, csrf = core_stack["login"](username)
        c = TestClient(api)
        c.cookies.set("nvb_session", session)
        c.headers["X-CSRF-Token"] = csrf
        return c, core_client

    yield {"api": api, "store": store, "relay": relay_broker, "worker": worker, "portal": portal, "sm": stm_sm,
           "s": stm_settings, "core": core_stack}
    for b in made + [relay_broker]:
        b.close()


def _request(portal, account_id, period="2026-10"):
    r = portal.post(f"{URL}/requests", json={"account_id": account_id, "period": period},
                    headers={"Idempotency-Key": uuid.uuid4().hex})
    assert r.status_code == 202, r.text
    return r.json()["id"]


def _relay(chain):
    from nvb_statements.relay import publish_pending

    return publish_pending(chain["sm"], chain["relay"], 100)


def _row(chain, rid):
    from nvb_statements.models import StatementRequest

    with chain["sm"]() as db:
        return db.get(StatementRequest, rid)


def test_statement_request_to_pdf_download_on_real_dependencies(chain):
    from pypdf import PdfReader

    portal, _ = chain["portal"]("customer001")
    accounts = portal.get(f"{URL}/accounts").json()["items"]
    rid = _request(portal, accounts[0]["account_id"])
    assert _relay(chain) == 1
    w = chain["worker"]()
    assert w.run(drain=True, max_messages=10) == 0               # real bounded drain run, exits on idle
    row = _row(chain, rid)
    assert row.status == "completed" and row.attempts == 1 and len(row.dataset_id) == 64
    assert chain["store"].exists(row.result_key)
    status = portal.get(f"{URL}/requests/{rid}").json()
    pdf = portal.get(status["document_url"])
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf.content)).pages)
    assert "SYNTHETIC DEMO DATA" in text and accounts[0]["masked_number"][-4:] in text
    assert w.broker.depth(chain["s"].queue_name) == 0

    other, _ = chain["portal"]("customer002")                    # another customer: no status, no file
    assert other.get(f"{URL}/requests/{rid}").status_code == 404
    assert other.get(status["document_url"]).status_code == 404
    chain["store"].delete(row.result_key)


def test_crash_after_upload_before_db_commit_recovers_to_one_object(chain):
    from nvb_statements.relay import reconcile
    from nvb_statements.worker import Crash

    portal, _ = chain["portal"]("customer001")
    rid = _request(portal, portal.get(f"{URL}/accounts").json()["items"][0]["account_id"])
    _relay(chain)
    crashing = chain["worker"]("crasher", crash_at="after_upload")
    d = crashing.broker.get(chain["s"].queue_name, 5)
    with pytest.raises(Crash):
        crashing.handle(d)                                        # object uploaded, DB still 'running', no ack
    crashing.broker.close()                                       # the process is gone -> broker redelivers
    row = _row(chain, rid)
    assert row.status == "running" and row.result_key is None
    orphan = chain["store"].list(f"statements/{rid}/")
    assert len(orphan) == 1                                       # uploaded but not recorded yet

    from datetime import timedelta

    from sqlalchemy import update

    from nvb_statements.models import StatementRequest, utcnow

    with chain["sm"]() as db:                                     # the dead worker's lease runs out
        db.execute(update(StatementRequest).where(StatementRequest.id == rid)
                   .values(lease_expires_at=utcnow() - timedelta(seconds=1)))
        db.commit()
    assert reconcile(chain["sm"], chain["s"].queue_name) == 1
    _relay(chain)
    survivor = chain["worker"]("survivor")
    assert survivor.run(drain=True, max_messages=10) == 0
    row = _row(chain, rid)
    assert row.status == "completed" and row.attempts == 2
    assert chain["store"].list(f"statements/{rid}/") == [row.result_key] == orphan    # same deterministic key
    assert portal.get(f"{URL}/requests/{rid}/document").status_code == 200
    chain["store"].delete(row.result_key)


def test_crash_after_db_commit_before_ack_is_acked_on_redelivery(chain):
    from nvb_statements.models import StatementEvent
    from nvb_statements.worker import Crash

    portal, _ = chain["portal"]("customer001")
    rid = _request(portal, portal.get(f"{URL}/accounts").json()["items"][0]["account_id"])
    _relay(chain)
    crashing = chain["worker"]("crasher", crash_at="before_ack")
    d = crashing.broker.get(chain["s"].queue_name, 5)
    with pytest.raises(Crash):
        crashing.handle(d)                                        # completed in the DB, ack never sent
    crashing.broker.close()
    assert _row(chain, rid).status == "completed"
    survivor = chain["worker"]("survivor")
    again = None
    for _ in range(15):
        again = survivor.broker.get(chain["s"].queue_name, 1)
        if again:
            break
    assert again is not None and again.redelivered is True
    assert survivor.handle(again) == "already_completed"          # idempotent: acked, nothing regenerated
    row = _row(chain, rid)
    assert row.attempts == 1 and chain["store"].list(f"statements/{rid}/") == [row.result_key]
    with chain["sm"]() as db:
        assert len(db.scalars(select(StatementEvent).where(StatementEvent.request_id == rid,
                                                           StatementEvent.event_type == "completed")).all()) == 1
    chain["store"].delete(row.result_key)


def test_duplicate_task_publication_produces_one_result(chain):
    from nvb_statements.service import enqueue

    portal, _ = chain["portal"]("customer001")
    rid = _request(portal, portal.get(f"{URL}/accounts").json()["items"][0]["account_id"])
    with chain["sm"]() as db:
        enqueue(db, rid, chain["s"].queue_name)                   # the same task published twice
        db.commit()
    assert _relay(chain) == 2
    w = chain["worker"]()
    assert w.run(drain=True, max_messages=10) == 0
    row = _row(chain, rid)
    assert row.status == "completed" and row.attempts == 1
    assert chain["store"].list(f"statements/{rid}/") == [row.result_key]
    chain["store"].delete(row.result_key)


def test_object_storage_outage_retries_then_succeeds_without_losing_the_task(chain):
    import socket

    from nvb_statements.relay import reconcile
    from nvb_statements.storage import S3Store

    portal, _ = chain["portal"]("customer001")
    rid = _request(portal, portal.get(f"{URL}/accounts").json()["items"][0]["account_id"])
    _relay(chain)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    dead_port = sock.getsockname()[1]
    sock.close()
    w = chain["worker"]("w-outage")
    w.store = S3Store(chain["s"].model_copy(update={"s3_endpoint_url": f"http://127.0.0.1:{dead_port}"}))
    assert w.handle(w.broker.get(chain["s"].queue_name, 5)) == "retry_scheduled"
    row = _row(chain, rid)
    assert row.status == "queued" and row.error_code == "storage_unavailable" and row.result_key is None
    assert portal.get(f"{URL}/requests/{rid}").json()["status"] == "queued"      # never shown as completed
    assert reconcile(chain["sm"], chain["s"].queue_name) == 1                     # backoff (0 s in tests) is due
    _relay(chain)
    healthy = chain["worker"]("w-ok")
    assert healthy.run(drain=True, max_messages=10) == 0
    row = _row(chain, rid)
    assert row.status == "completed" and row.attempts == 2
    chain["store"].delete(row.result_key)


def test_core_unavailable_never_produces_a_partial_statement(chain):
    """Adapter/core down: bounded retries, then a clear failure. No object is ever written."""
    from nvb_platform.adapter_client import AdapterCallError
    from nvb_statements.relay import reconcile

    portal, _ = chain["portal"]("customer001")
    rid = _request(portal, portal.get(f"{URL}/accounts").json()["items"][0]["account_id"])
    _relay(chain)
    w = chain["worker"]("w-core-down")

    class Down:
        def manifest(self, *a, **kw):
            raise AdapterCallError(503, "core_timeout", "The core did not respond in time.", True)

        def close(self):
            pass

    w.adapter = Down()
    outcomes = []
    for _ in range(chain["s"].max_attempts):
        d = w.broker.get(chain["s"].queue_name, 5)
        outcomes.append(w.handle(d))
        reconcile(chain["sm"], chain["s"].queue_name)
        _relay(chain)
    assert outcomes == ["retry_scheduled", "retry_scheduled", "failed"]
    row = _row(chain, rid)
    assert row.status == "failed" and row.error_code == "attempts_exhausted"
    assert chain["store"].list(f"statements/{rid}/") == []
    body = portal.get(f"{URL}/requests/{rid}").json()
    assert body["status"] == "failed" and body["document_url"] is None
    assert w.broker.get(chain["s"].queue_name, 0.5) is None                       # no poison loop left behind


def test_logout_and_cross_customer_checks_on_the_real_stack(chain):
    portal, core_client = chain["portal"]("customer001")
    rid = _request(portal, portal.get(f"{URL}/accounts").json()["items"][0]["account_id"])
    assert portal.get(f"{URL}/requests/{rid}").status_code == 200
    csrf = portal.headers["X-CSRF-Token"]
    portal.headers["X-CSRF-Token"] = "wrong"
    assert portal.post(f"{URL}/requests/{rid}/cancel").status_code == 403         # invalid CSRF
    portal.headers["X-CSRF-Token"] = csrf
    assert core_client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert portal.get(f"{URL}/requests/{rid}").status_code == 401                 # session ended at the core
    assert portal.post(f"{URL}/requests/{rid}/cancel").status_code == 401


def test_batch_on_real_dependencies_with_authorised_discovery(chain):
    """Manual batch (role batch_operator) -> frozen manifest from the core's discovery ->
    members on the batch queue -> bounded worker runs -> progress -> completed."""
    from nvb_platform import roles as roles_lib
    from nvb_statements.models import KNOWN_ROLES, RoleAssignment, RoleEvent

    staff, _ = None, None
    core_client, session, csrf = chain["core"]["login"]("opsadmin", "admin")
    from fastapi.testclient import TestClient

    staff = TestClient(chain["api"])
    staff.cookies.set("nvb_session", session)
    staff.headers["X-CSRF-Token"] = csrf
    key = {"Idempotency-Key": uuid.uuid4().hex}
    assert staff.post(f"{URL}/batches", json={"period": "2026-10"}, headers=key).status_code == 403   # no role yet
    subject = staff.get(f"{URL}/me").json()["subject"]
    with chain["sm"]() as db:
        roles_lib.set_role(db, RoleAssignment, RoleEvent, subject=subject, role="batch_operator", active=True,
                           actor="test:bootstrap", known_roles=KNOWN_ROLES)
        db.commit()
    r = staff.post(f"{URL}/batches", json={"period": "2026-10"}, headers=key)
    assert r.status_code == 202, r.text
    batch = r.json()
    total = batch["progress"]["total"]
    assert batch["status"] == "running" and total >= 3 and batch["progress"]["queued"] == total
    assert _relay(chain) == total
    first = chain["worker"]("job-1")
    assert first.run(drain=True, max_messages=2) == 0             # one bounded Job handles 2 members
    mid = staff.get(f"{URL}/batches/{batch['id']}").json()
    assert mid["status"] == "running" and mid["progress"]["completed"] == 2
    second = chain["worker"]("job-2")
    assert second.run(drain=True, max_messages=100) == 0
    done = staff.get(f"{URL}/batches/{batch['id']}").json()
    assert done["status"] == "completed" and done["progress"]["completed"] == total and done["failures"] == []
    from nvb_statements.models import StatementRequest

    with chain["sm"]() as db:
        keys = db.scalars(select(StatementRequest.result_key).where(StatementRequest.batch_id == batch["id"])).all()
    assert len(keys) == total and all(chain["store"].exists(k) for k in keys)
    for k in keys:
        chain["store"].delete(k)
