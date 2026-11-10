"""Statements on real PostgreSQL: migrations, least-privilege grants, locking and concurrency."""

from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import psycopg
import pytest
from sqlalchemy import func, select

from tests.integration.conftest import app_database_url, read, reset_schema, run_migration

SEPT = ("2026-09", "Africa/Cairo")


def _period():
    from nvb_statements.period import month_period

    return month_period(*SEPT)


def _account(n):
    return {"account_id": n, "masked_number": f"•••• {n:04d}"}


def _new_request(sm, queue, account_id=11, subject="nvb-user:1:1700000000", key=None):
    from nvb_statements.service import create_request

    with sm() as db:
        return create_request(db, owner_subject=subject, requested_by=subject, account=_account(account_id),
                              period=_period(), idempotency_key=key or uuid.uuid4().hex, queue=queue)


# --- migrations ---------------------------------------------------------------------------


def test_migration_from_empty_database_to_head(stm_owner_kw, stm_migrated):
    with psycopg.connect(**stm_owner_kw) as conn:
        tables = {r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")}
        version = conn.execute("SELECT version_num FROM alembic_version_statements").fetchone()[0]
    assert {"statement_requests", "statement_events", "outbox_messages", "statement_batches", "batch_items",
            "role_assignments", "role_events", "alembic_version_statements"} <= tables
    assert version == "0002_statements_batches_roles"


def test_upgrade_0001_to_head_keeps_existing_rows(stm_owner_kw):
    """Upgrade path of a database created by image 0.1.x: rows survive and get the default queue."""
    url, pw = os.environ["TEST_STATEMENTS_DATABASE_URL"], os.environ["TEST_STATEMENTS_DATABASE_PASSWORD_FILE"]
    role = os.environ.get("TEST_STATEMENTS_APP_ROLE")
    reset_schema(stm_owner_kw)
    try:
        run_migration("statements", url, pw, role, "0001_statements_initial")
        now = datetime.now(UTC)
        with psycopg.connect(**stm_owner_kw) as conn:
            conn.execute(
                "INSERT INTO statement_requests (id, owner_subject, requested_by_subject, purpose, account_id, "
                "account_masked, data_scope, period_label, period_timezone, period_start_utc, period_end_utc, "
                "dataset_version, status, attempts, idempotency_key, request_fingerprint, created_at, updated_at) "
                "VALUES ('old-1','s','s','customer_request',1,'x','statement:single_account','2026-09','UTC',%s,%s,"
                "'statement-v1','completed',1,'k-00000001','f',%s,%s)", (now, now, now, now))
        run_migration("statements", url, pw, role)   # 0001 -> head
        run_migration("statements", url, pw, role)   # re-running at head is a no-op
        with psycopg.connect(**stm_owner_kw) as conn:
            row = conn.execute("SELECT status, queue FROM statement_requests WHERE id='old-1'").fetchone()
        assert row == ("completed", "nvb.statements.generate")
    finally:  # leave a clean head schema for the other tests
        reset_schema(stm_owner_kw)
        run_migration("statements", url, pw, role)


def test_downgrade_is_refused(stm_migrated):
    import subprocess
    import sys

    from tests.integration.conftest import REPO

    env = {**os.environ, "STATEMENTS_MIGRATION_DATABASE_URL": os.environ["TEST_STATEMENTS_DATABASE_URL"],
           "STATEMENTS_MIGRATION_PASSWORD_FILE": os.environ["TEST_STATEMENTS_DATABASE_PASSWORD_FILE"]}
    out = subprocess.run([sys.executable, "-m", "alembic", "-c", str(REPO / "migrations/statements/alembic.ini"),
                          "downgrade", "base"], env=env, capture_output=True, text=True, cwd=str(REPO))
    assert out.returncode != 0 and "Automatic downgrade is disabled" in out.stdout + out.stderr


def test_app_role_is_least_privilege(stm_clean):
    """The runtime role can work, cannot rewrite audit history and cannot change the schema."""
    if not os.environ.get("TEST_STATEMENTS_APP_ROLE"):
        pytest.skip("no separate application role provisioned")
    url, pw_file = app_database_url("statements")
    from urllib.parse import urlsplit

    p = urlsplit(url)
    kw = dict(host=p.hostname, port=p.port, dbname=p.path.lstrip("/"), user=p.username,
              password=read("TEST_STATEMENTS_APP_PASSWORD_FILE"), autocommit=True)
    now = datetime.now(UTC)
    with psycopg.connect(**kw) as conn:
        conn.execute("INSERT INTO statement_events (request_id, event_type, actor, details, created_at) "
                     "VALUES ('r','t','a','{}',%s)", (now,))
        conn.execute("INSERT INTO role_events (subject, role, action, actor, reason, created_at) "
                     "VALUES ('s','batch_operator','grant','a','',%s)", (now,))
        for sql in ("UPDATE statement_events SET actor='x'", "DELETE FROM statement_events",
                    "UPDATE role_events SET actor='x'", "DELETE FROM role_events",
                    "DELETE FROM statement_requests", "TRUNCATE outbox_messages",
                    "DROP TABLE statement_requests", "CREATE TABLE evil (id int)",
                    "ALTER TABLE statement_requests ADD COLUMN x int"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(sql)


# --- concurrency ---------------------------------------------------------------------------


def test_concurrent_creates_with_the_same_idempotency_key_yield_one_request(stm_sm, stm_settings):
    from nvb_statements.models import OutboxMessage, StatementRequest

    key = uuid.uuid4().hex
    barrier = threading.Barrier(8)

    def go(_):
        barrier.wait()
        return _new_request(stm_sm, stm_settings.queue_name, key=key)

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(go, range(8)))
    assert len({r.request.id for r in results}) == 1
    assert sum(1 for r in results if not r.replayed) == 1
    with stm_sm() as db:
        assert db.scalar(select(func.count()).select_from(StatementRequest)) == 1
        assert db.scalar(select(func.count()).select_from(OutboxMessage)) == 1     # exactly one task published


def test_concurrent_claims_have_exactly_one_winner(stm_sm, stm_settings):
    from nvb_platform.broker import InMemoryBroker
    from nvb_statements.models import StatementRequest
    from nvb_statements.storage import InMemoryStore
    from nvb_statements.worker import Worker

    rid = _new_request(stm_sm, stm_settings.queue_name).request.id
    barrier = threading.Barrier(6)

    def claim(i):
        w = Worker(stm_settings, stm_sm, InMemoryBroker(), InMemoryStore(), None, worker_id=f"w{i}")
        barrier.wait()
        with stm_sm() as db:
            return w._claim(db, rid) is not None

    with ThreadPoolExecutor(6) as pool:
        wins = list(pool.map(claim, range(6)))
    assert wins.count(True) == 1
    with stm_sm() as db:
        r = db.get(StatementRequest, rid)
        assert r.status == "running" and r.attempts == 1 and r.lease_owner.startswith("w")


def test_two_relays_publish_every_outbox_row_exactly_once(stm_sm, stm_settings):
    """FOR UPDATE SKIP LOCKED: concurrent relays never take the same row."""
    from nvb_platform.broker import InMemoryBroker
    from nvb_statements.models import OutboxMessage
    from nvb_statements.relay import publish_pending

    for i in range(60):
        _new_request(stm_sm, stm_settings.queue_name, account_id=100 + i)
    brokers = [InMemoryBroker(), InMemoryBroker(), InMemoryBroker()]
    barrier = threading.Barrier(3)

    def relay(b):
        barrier.wait()
        total = 0
        while (n := publish_pending(stm_sm, b, 7)):
            total += n
        return total

    with ThreadPoolExecutor(3) as pool:
        counts = list(pool.map(relay, brokers))
    published = [m for b in brokers for m in b.published]
    assert sum(counts) == 60 and len(published) == 60 and len(set(published)) == 60
    with stm_sm() as db:
        assert db.scalar(select(func.count()).select_from(OutboxMessage)
                         .where(OutboxMessage.published_at.is_(None))) == 0


def test_concurrent_reconcilers_requeue_an_expired_lease_once(stm_sm, stm_settings):
    from datetime import timedelta

    from sqlalchemy import update

    from nvb_statements.models import OutboxMessage, StatementRequest, utcnow
    from nvb_statements.relay import reconcile

    rid = _new_request(stm_sm, stm_settings.queue_name).request.id
    with stm_sm() as db:
        db.execute(update(OutboxMessage).values(published_at=utcnow()))
        db.execute(update(StatementRequest).where(StatementRequest.id == rid)
                   .values(status="running", lease_owner="dead", lease_expires_at=utcnow() - timedelta(seconds=5)))
        db.commit()
    barrier = threading.Barrier(4)

    def go(_):
        barrier.wait()
        return reconcile(stm_sm, stm_settings.queue_name)

    with ThreadPoolExecutor(4) as pool:
        assert sum(pool.map(go, range(4))) == 1
    with stm_sm() as db:
        assert db.get(StatementRequest, rid).status == "queued"
        assert db.scalar(select(func.count()).select_from(OutboxMessage)
                         .where(OutboxMessage.published_at.is_(None))) == 1


class _Discovery:
    """Minimal adapter double for batch discovery (the adapter itself is covered elsewhere)."""

    def __init__(self, n):
        self.accounts = list(range(1000, 1000 + n))

    def discovery(self, after, limit, request_id=None, include_load_test=False):
        ids = [a for a in self.accounts if a > after]
        chunk = ids[:limit]
        more = len(ids) > limit
        return {"items": [{"account_id": a, "subject": f"nvb-user:{a}:1700000000", "masked_number": f"•••• {a}"}
                          for a in chunk], "has_more": more, "next_after_account_id": chunk[-1] if more else None}


def test_concurrent_batch_advance_freezes_and_releases_each_member_once(stm_sm, stm_settings):
    """Two callers (e.g. an API retry racing the CronJob) advancing the same batch."""
    from nvb_statements import batch as batch_ops
    from nvb_statements.models import BatchItem, OutboxMessage, StatementBatch, StatementRequest

    s = stm_settings.model_copy(update={"batch_discovery_page_size": 7, "batch_release_chunk": 9})
    with stm_sm() as db:
        b, _ = batch_ops.create_batch(db, s, period_label="2026-09", created_by="system:schedule",
                                      trigger="schedule", idempotency_key="schedule-2026-09")
    batch_id = b.id
    barrier = threading.Barrier(4)

    def go(_):
        barrier.wait()
        return batch_ops.advance_batch(stm_sm, _Discovery(53), s, batch_id).status

    with ThreadPoolExecutor(4) as pool:
        statuses = list(pool.map(go, range(4)))
    assert set(statuses) == {"running"}
    with stm_sm() as db:
        batch = db.get(StatementBatch, batch_id)
        assert batch.total_items == 53 and batch.released_items == 53
        assert db.scalar(select(func.count()).select_from(BatchItem)) == 53
        assert db.scalar(select(func.count()).select_from(StatementRequest)) == 53
        assert db.scalar(select(func.count(func.distinct(StatementRequest.account_id)))) == 53
        assert db.scalar(select(func.count()).select_from(OutboxMessage)) == 53


def test_concurrent_batch_creation_with_one_key_creates_one_batch(stm_sm, stm_settings):
    from nvb_statements import batch as batch_ops
    from nvb_statements.models import StatementBatch

    barrier = threading.Barrier(6)

    def go(_):
        barrier.wait()
        with stm_sm() as db:
            return batch_ops.create_batch(db, stm_settings, period_label="2026-09", created_by="system:schedule",
                                          trigger="schedule", idempotency_key="schedule-2026-09")[0].id

    with ThreadPoolExecutor(6) as pool:
        ids = set(pool.map(go, range(6)))
    assert len(ids) == 1
    with stm_sm() as db:
        assert db.scalar(select(func.count()).select_from(StatementBatch)) == 1


def test_database_outage_is_reported_by_readiness_and_never_acked(stm_settings, tmp_path):
    """Unreachable database: API readiness is 503 and the worker requeues instead of acking."""
    from fastapi.testclient import TestClient

    from nvb_platform.broker import InMemoryBroker
    from nvb_statements.api import create_app
    from nvb_statements.db import build_engine, build_sessionmaker
    from nvb_statements.storage import InMemoryStore
    from nvb_statements.worker import Worker

    down = stm_settings.model_copy(update={"database_url": "postgresql+psycopg://nobody@127.0.0.1:9/none"})
    sm = build_sessionmaker(build_engine(down))
    api = create_app(down, adapter=object(), store=InMemoryStore(), sessionmaker=sm)
    r = TestClient(api).get("/health/ready")
    assert r.status_code == 503 and r.json()["problems"] == ["database"]
    assert TestClient(api).get("/health/live").status_code == 200

    broker = InMemoryBroker()
    broker.publish(down.queue_name, "m1", {"type": "statement.generate", "request_id": str(uuid.uuid4())})
    worker = Worker(down, sm, broker, InMemoryStore(), None, worker_id="w")
    assert worker.run(drain=True, max_messages=5) == 2          # non-zero exit, no ack
    assert broker.depth(down.queue_name) == 1 and not broker.unacked
