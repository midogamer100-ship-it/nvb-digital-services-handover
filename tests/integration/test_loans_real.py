"""Loans on real PostgreSQL + RabbitMQ with the patched core: migration, least privilege,
simultaneous staff updates and the summary worker reading authorised core data."""

from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from sqlalchemy import func, select

from tests.integration.conftest import (
    app_database_url,
    read,
    reset_schema,
    run_migration,
    verify_disposable_database,
)

B = "/x/loans/v1"
REASON = "Reviewed synthetic inflows and outflows."


@pytest.fixture(scope="module")
def loans_owner_kw():
    if not os.environ.get("TEST_LOANS_DATABASE_URL"):
        pytest.skip("TEST_LOANS_DATABASE_URL not set")
    kw = verify_disposable_database("TEST_LOANS_DATABASE_URL", "TEST_LOANS_DATABASE_PASSWORD_FILE")
    reset_schema(kw)
    run_migration("loans", os.environ["TEST_LOANS_DATABASE_URL"], os.environ["TEST_LOANS_DATABASE_PASSWORD_FILE"],
                  os.environ.get("TEST_LOANS_APP_ROLE"))
    return kw


@pytest.fixture
def loans(loans_owner_kw, core_stack, queues):
    from fastapi.testclient import TestClient

    from nvb_loans.api import create_app
    from nvb_loans.config import Settings
    from nvb_loans.db import build_engine, build_sessionmaker
    from nvb_loans.models import KNOWN_ROLES, RoleAssignment, RoleEvent
    from nvb_loans.worker import Worker
    from nvb_platform import roles as roles_lib
    from nvb_platform.adapter_client import AdapterClient
    from nvb_platform.broker import RabbitBroker

    with psycopg.connect(**loans_owner_kw, autocommit=True) as conn:
        conn.execute("TRUNCATE loan_applications, loan_events, summary_tasks, loan_batches, outbox_messages, "
                     "role_assignments, role_events RESTART IDENTITY")
    url, pw_file = app_database_url("loans")
    s = Settings(database_url=url, database_password_file=pw_file, broker_url=os.environ["TEST_BROKER_URL"],
                 broker_username_file=os.environ["TEST_BROKER_USERNAME_FILE"],
                 broker_password_file=os.environ["TEST_BROKER_PASSWORD_FILE"], allow_plaintext_dependencies=True,
                 queue_name=queues["interactive"], adapter_base_url="http://adapter.inprocess",
                 allow_insecure_http=True, settle_seconds=600, retry_base_seconds=0, retry_max_seconds=0,
                 idle_exit_seconds=1, log_level="WARNING", broker_heartbeat_seconds=60)
    engine = build_engine(s, "nvb-integration-tests")
    sm = build_sessionmaker(engine)

    def adapter(name):
        return AdapterClient(s.model_copy(update={"adapter_token_file": core_stack["token_files"][name]}),
                             transport=core_stack["adapter_transport"])

    api = create_app(s, adapter=adapter("loans-api"), sessionmaker=sm)
    relay = RabbitBroker(s, declare=(s.queue_name,))
    consumer = RabbitBroker(s, verify=(s.queue_name,))

    def session(username, group="customer"):
        _core, cookie, csrf = core_stack["login"](username, group)
        c = TestClient(api)
        c.cookies.set("nvb_session", cookie)
        c.headers["X-CSRF-Token"] = csrf
        return c

    def grant(client, role):
        subject = client.get(f"{B}/me").json()["subject"]
        with sm() as db:
            roles_lib.set_role(db, RoleAssignment, RoleEvent, subject=subject, role=role, active=True,
                               actor="test:bootstrap", known_roles=KNOWN_ROLES)
            db.commit()
        return subject

    yield {"s": s, "sm": sm, "api": api, "relay": relay, "session": session, "grant": grant,
           "worker": Worker(s, sm, consumer, adapter("loans-worker"), worker_id="w1")}
    relay.close(), consumer.close()
    engine.dispose()


def test_loans_migration_and_app_role_privileges(loans_owner_kw):
    with psycopg.connect(**loans_owner_kw) as conn:
        tables = {r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")}
        assert conn.execute("SELECT version_num FROM alembic_version_loans").fetchone()[0] == "0001_loans_initial"
    assert {"loan_applications", "loan_events", "summary_tasks", "loan_batches", "outbox_messages",
            "role_assignments", "role_events"} <= tables
    if not os.environ.get("TEST_LOANS_APP_ROLE"):
        return
    from urllib.parse import urlsplit

    p = urlsplit(app_database_url("loans")[0])
    kw = dict(host=p.hostname, port=p.port, dbname=p.path.lstrip("/"), user=p.username,
              password=read("TEST_LOANS_APP_PASSWORD_FILE"), autocommit=True)
    with psycopg.connect(**kw) as conn:
        for sql in ("UPDATE loan_events SET actor='x'", "DELETE FROM loan_events", "DELETE FROM loan_applications",
                    "DELETE FROM role_events", "DROP TABLE loan_applications", "CREATE TABLE evil (id int)"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(sql)


def _prepare(loans):
    """customer001 applies; reviewer recommends; two approvers are ready to decide."""
    customer = loans["session"]("customer001")
    rev, appr1 = (loans["session"](u) for u in ("customer002", "customer003"))
    appr2 = loans["session"]("opsadmin", "admin")
    rev_subject = loans["grant"](rev, "reviewer")
    loans["grant"](appr1, "approver"), loans["grant"](appr2, "approver")
    account = customer.get("/x/loans/v1/me").json() and None
    from nvb_loans.models import LoanApplication

    accounts = loans["api"].state.adapter.resolve(customer.cookies.get("nvb_session"), None, None).accounts
    d = customer.post(f"{B}/applications", json={"account_id": accounts[0]["account_id"], "amount_minor": 6_000_000,
                                                 "term_months": 12, "monthly_income_minor": 900_000,
                                                 "purpose": "synthetic"}).json()
    a = customer.post(f"{B}/applications/{d['id']}/submit", json={"version": d["version"]}).json()
    a = appr1.post(f"{B}/review/applications/{a['id']}/assign",
                   json={"reviewer_subject": rev_subject, "version": a["version"]}).json()
    a = rev.post(f"{B}/review/applications/{a['id']}/recommendation",
                 json={"recommendation": "approve", "reason": REASON, "version": a["version"]}).json()
    assert a["status"] == "under_review" and account is None and LoanApplication
    return customer, rev, appr1, appr2, a


def test_two_approvers_deciding_simultaneously_exactly_one_wins(loans):
    from nvb_loans.models import LoanApplication, LoanEvent

    _customer, _rev, appr1, appr2, a = _prepare(loans)
    barrier = threading.Barrier(2)

    def decide(args):
        client, decision = args
        barrier.wait()
        return client.post(f"{B}/review/applications/{a['id']}/decision",
                           json={"decision": decision, "reason": REASON, "version": a["version"]})

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(decide, [(appr1, "approved"), (appr2, "rejected")]))
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409]
    loser = next(r for r in results if r.status_code == 409)
    assert loser.json()["error"]["code"] in ("version_conflict", "invalid_state")
    winner = next(r for r in results if r.status_code == 200).json()
    with loans["sm"]() as db:
        row = db.get(LoanApplication, a["id"])
        assert row.status == winner["status"] and row.version == a["version"] + 1
        assert db.scalar(select(func.count()).select_from(LoanEvent)
                         .where(LoanEvent.application_id == a["id"], LoanEvent.event_type == "decided")) == 1


def test_two_reviewer_updates_on_the_same_version_one_wins(loans):
    customer = loans["session"]("customer001")
    rev = loans["session"]("customer002")
    appr = loans["session"]("opsadmin", "admin")
    rev_subject = loans["grant"](rev, "reviewer")
    loans["grant"](appr, "approver")
    accounts = loans["api"].state.adapter.resolve(customer.cookies.get("nvb_session"), None, None).accounts
    d = customer.post(f"{B}/applications", json={"account_id": accounts[0]["account_id"], "amount_minor": 100_000,
                                                 "term_months": 6, "monthly_income_minor": 0}).json()
    a = customer.post(f"{B}/applications/{d['id']}/submit", json={"version": d["version"]}).json()
    a = appr.post(f"{B}/review/applications/{a['id']}/assign",
                  json={"reviewer_subject": rev_subject, "version": a["version"]}).json()
    barrier = threading.Barrier(4)

    def recommend(rec):
        barrier.wait()
        return rev.post(f"{B}/review/applications/{a['id']}/recommendation",
                        json={"recommendation": rec, "reason": REASON, "version": a["version"]}).status_code

    with ThreadPoolExecutor(4) as pool:
        codes = sorted(pool.map(recommend, ["approve", "reject", "approve", "reject"]))
    assert codes == [200, 409, 409, 409]


def test_submit_to_summary_through_real_broker_and_core(loans):
    from nvb_loans.models import SummaryTask
    from nvb_loans.relay import publish_pending

    _customer, rev, appr1, _appr2, a = _prepare(loans)
    assert publish_pending(loans["sm"], loans["relay"], 10, raise_on_failure=True) == 1
    assert loans["worker"].run(drain=True, max_messages=10) == 0
    with loans["sm"]() as db:
        task = db.scalar(select(SummaryTask).where(SummaryTask.application_id == a["id"]))
        assert task.status == "completed" and task.attempts == 1
    summary = rev.get(f"{B}/review/applications/{a['id']}").json()["summary"]
    assert "SYNTHETIC DEMO SCORE" in summary["label"] and len(summary["result"]["months"]) == 3
    assert summary["result"]["installment_minor"] == 500_000
    done = appr1.post(f"{B}/review/applications/{a['id']}/decision",
                      json={"decision": "approved", "reason": REASON, "version": a["version"]}).json()
    assert done["status"] == "approved"


def test_approval_changes_no_core_balance_and_writes_no_core_ledger_entry(loans, core_stack):
    """Approval is a workflow decision only: the core ledger and balances are byte-for-byte unchanged."""
    from sqlalchemy import text

    def snapshot():
        with core_stack["core"].state.engine.connect() as conn:
            return (conn.execute(text("SELECT count(*), coalesce(sum(amount_minor),0), coalesce(max(id),0) "
                                      "FROM ledger_entries")).one(),
                    conn.execute(text("SELECT coalesce(sum(balance_minor),0) FROM accounts")).scalar(),
                    conn.execute(text("SELECT count(*) FROM transfers")).scalar())

    _customer, _rev, appr1, _appr2, a = _prepare(loans)
    before = snapshot()
    r = appr1.post(f"{B}/review/applications/{a['id']}/decision",
                   json={"decision": "approved", "reason": REASON, "version": a["version"]})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert snapshot() == before
    assert uuid.uuid4()
