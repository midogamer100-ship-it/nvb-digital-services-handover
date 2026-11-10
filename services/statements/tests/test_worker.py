"""Worker / relay reliability: crash points, duplicates, retries, outbox recovery, PDF content."""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pypdf import PdfReader
from sqlalchemy import func, select, update

from nvb_statements.models import OutboxMessage, StatementEvent, StatementRequest, utcnow
from nvb_statements.relay import cleanup_orphans, publish_pending, reconcile
from nvb_statements.worker import Crash, Worker
from tests.conftest import SEPT_END, SEPT_START, browser

URL = "/x/statements/v1/requests"


def new_request(app, fake, account_id=11, period="2026-09"):
    r = browser(app, fake).post(URL, json={"account_id": account_id, "period": period},
                                headers={"Idempotency-Key": uuid.uuid4().hex})
    assert r.status_code == 202, r.text
    return r.json()["id"]


def status(sm, rid):
    with sm() as db:
        return db.get(StatementRequest, rid)


def pdf_text(data: bytes) -> str:
    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(data)).pages)


def drain(sm, broker, worker):
    publish_pending(sm, broker, 100)
    out = []
    while (d := broker.get(worker.s.queue_name, 0)) is not None:
        out.append(worker.handle(d))
    return out


def expire_lease(sm, rid):
    with sm() as db:
        db.execute(update(StatementRequest).where(StatementRequest.id == rid)
                   .values(lease_expires_at=utcnow() - timedelta(seconds=1)))
        db.commit()


# --- happy path and PDF content ----------------------------------------------------------


def test_statement_end_to_end_contents(app, fake, sm, broker, worker, store):
    rid = new_request(app, fake)
    assert drain(sm, broker, worker) == ["completed"]
    r = status(sm, rid)
    assert r.status == "completed" and r.attempts == 1 and r.entry_count == 3
    assert r.result_key == f"statements/{rid}/{r.dataset_id}.pdf"
    text = pdf_text(store.get(r.result_key))
    assert "SYNTHETIC DEMO DATA" in text
    assert "•••• 1111" in text or "1111" in text
    assert "1,000.00 EGP" in text          # opening: only the August posting
    assert "500.50" in text                # posting exactly at period start is included
    assert "999.99" not in text            # posting exactly at period end is excluded
    assert "1,377.04 EGP" in text          # closing = 1000.00 + 500.50 - 123.45 - 0.01
    assert "Synthetic <Holder> & Co" in text  # escaped, rendered literally
    assert "Africa/Cairo" in text and "2026-08-31T21:00:00+00:00" in text
    assert broker.unacked == {}


def test_empty_period_renders(app, fake, sm, broker, worker, store):
    rid = new_request(app, fake, account_id=12)
    assert drain(sm, broker, worker) == ["completed"]
    text = pdf_text(store.get(status(sm, rid).result_key))
    assert "No transactions in this period." in text and "0.00 EGP" in text


def test_more_than_ten_thousand_entries(app, fake, sm, broker, settings, store):
    for i in range(10_050):
        fake.post(12, SEPT_START + timedelta(seconds=i * 7), "credit", 1)
    rid = new_request(app, fake, account_id=12)
    w = Worker(settings.model_copy(update={"entries_page_size": 1000}), sm, broker, store, fake, worker_id="big")
    assert drain(sm, broker, w) == ["completed"]
    r = status(sm, rid)
    assert r.entry_count == 10_050
    assert fake.calls.count("entries") == 11
    assert "100.50 EGP" in pdf_text(store.get(r.result_key))


# --- crash points -------------------------------------------------------------------------


def test_crash_after_dequeue_is_recovered_after_lease_expiry(app, fake, sm, broker, settings, store):
    rid = new_request(app, fake)
    publish_pending(sm, broker, 100)
    crashing = Worker(settings, sm, broker, store, fake, worker_id="dies", crash_at="after_claim")
    d = broker.get(settings.queue_name, 0)
    with pytest.raises(Crash):
        crashing.handle(d)
    broker.crash_consumer()                       # connection lost -> broker redelivers
    healthy = Worker(settings, sm, broker, store, fake, worker_id="w2")
    assert drain(sm, broker, healthy) == ["duplicate"]   # live lease: not stolen
    expire_lease(sm, rid)
    assert reconcile(sm, settings.queue_name) == 1
    assert drain(sm, broker, healthy) == ["completed"]
    assert status(sm, rid).attempts == 2


def test_crash_after_upload_before_db_completion_no_duplicate_file(app, fake, sm, broker, settings, store):
    rid = new_request(app, fake)
    publish_pending(sm, broker, 100)
    w = Worker(settings, sm, broker, store, fake, worker_id="dies", crash_at="after_upload")
    with pytest.raises(Crash):
        w.handle(broker.get(settings.queue_name, 0))
    broker.crash_consumer()
    assert status(sm, rid).status == "running"
    expire_lease(sm, rid)
    reconcile(sm, settings.queue_name)
    w2 = Worker(settings, sm, broker, store, fake, worker_id="w2")
    while (d := broker.get(settings.queue_name, 0)) is not None:
        w2.handle(d)
    publish_pending(sm, broker, 100)
    while (d := broker.get(settings.queue_name, 0)) is not None:
        w2.handle(d)
    r = status(sm, rid)
    assert r.status == "completed"
    assert store.list(f"statements/{rid}/") == [r.result_key]   # one logical file
    assert store.puts == 2


def test_crash_after_db_completion_before_ack(app, fake, sm, broker, settings, store):
    rid = new_request(app, fake)
    publish_pending(sm, broker, 100)
    w = Worker(settings, sm, broker, store, fake, worker_id="dies", crash_at="before_ack")
    with pytest.raises(Crash):
        w.handle(broker.get(settings.queue_name, 0))
    broker.crash_consumer()
    assert status(sm, rid).status == "completed"
    w2 = Worker(settings, sm, broker, store, fake, worker_id="w2")
    assert w2.handle(broker.get(settings.queue_name, 0)) == "already_completed"
    assert store.puts == 1 and broker.unacked == {}


def test_duplicate_delivery_single_outcome(app, fake, sm, broker, worker, store, settings):
    rid = new_request(app, fake)
    with sm() as db:  # simulate a relay crash after publish: the same request is enqueued twice
        from nvb_statements.service import enqueue
        enqueue(db, rid, settings.queue_name)
        db.commit()
    assert sorted(drain(sm, broker, worker)) == ["already_completed", "completed"]
    assert store.puts == 1
    with sm() as db:
        assert db.scalar(select(func.count()).select_from(StatementEvent)
                         .where(StatementEvent.event_type == "completed")) == 1


def test_unexpected_error_is_not_acked(app, fake, sm, broker, worker, settings, monkeypatch):
    new_request(app, fake)
    publish_pending(sm, broker, 100)

    def boom(*a, **k):
        raise RuntimeError("database went away")

    monkeypatch.setattr(worker, "_process", boom)
    assert worker.run(drain=True, max_messages=None) == 2
    assert broker.depth(settings.queue_name) == 1      # requeued, not acknowledged as success


# --- outbox / relay -----------------------------------------------------------------------


def test_broker_outage_keeps_outbox_and_recovers(app, fake, sm, broker, worker):
    rid = new_request(app, fake)
    broker.fail_publish = True
    assert publish_pending(sm, broker, 100) == 0
    with sm() as db:
        row = db.scalar(select(OutboxMessage))
        assert row.published_at is None and row.publish_attempts == 1 and row.last_error
    broker.fail_publish = False
    assert drain(sm, broker, worker) == ["completed"]
    assert status(sm, rid).status == "completed"


def test_relay_crash_after_publish_before_mark_gives_duplicate_not_loss(app, fake, sm, broker, worker):
    rid = new_request(app, fake)
    with pytest.raises(RuntimeError):
        publish_pending(sm, broker, 100, crash_after_publish=True)
    assert broker.depth(worker.s.queue_name) == 1
    assert sorted(drain(sm, broker, worker)) == ["already_completed", "completed"]
    assert status(sm, rid).status == "completed"


def test_stale_queued_request_is_reenqueued(app, fake, sm, broker, worker, settings):
    rid = new_request(app, fake)
    publish_pending(sm, broker, 100)
    broker.queues[settings.queue_name].clear()   # message lost (e.g. dead-lettered)
    with sm() as db:
        db.execute(update(StatementRequest).values(updated_at=utcnow() - timedelta(hours=1)))
        db.commit()
    assert reconcile(sm, settings.queue_name, stale_seconds=900) == 1
    assert drain(sm, broker, worker) == ["completed"]


# --- retries and terminal failures ----------------------------------------------------------


def test_transient_core_errors_retry_with_backoff_then_succeed(app, fake, sm, broker, worker, settings):
    rid = new_request(app, fake)
    fake.fail_manifest = 1
    assert drain(sm, broker, worker) == ["retry_scheduled"]
    r = status(sm, rid)
    assert r.status == "queued" and r.error_code == "core_busy" and r.not_before is not None
    assert reconcile(sm, settings.queue_name) == 1
    assert drain(sm, broker, worker) == ["completed"]


def test_retries_are_bounded(app, fake, sm, broker, worker, settings):
    rid = new_request(app, fake)
    fake.unavailable = True
    outcomes = []
    for _ in range(5):
        outcomes += drain(sm, broker, worker)
        reconcile(sm, settings.queue_name)
    r = status(sm, rid)
    assert r.status == "failed" and r.error_code == "attempts_exhausted" and r.attempts == settings.max_attempts
    assert outcomes.count("retry_scheduled") == settings.max_attempts - 1


def test_dataset_change_during_extraction_is_retried_not_rendered(app, fake, sm, broker, worker, settings, store):
    rid = new_request(app, fake)
    fake.mutate_after_first_page = True
    assert drain(sm, broker, worker) == ["retry_scheduled"]
    assert status(sm, rid).error_code == "dataset_incomplete" and store.puts == 0
    reconcile(sm, settings.queue_name)
    assert drain(sm, broker, worker) == ["completed"]
    assert status(sm, rid).entry_count == 4


def test_revoked_authorization_fails_without_retry(app, fake, sm, broker, worker):
    rid = new_request(app, fake)
    fake.owners[11] = "nvb-user:9:1"   # account no longer belongs to the requester at the core
    assert drain(sm, broker, worker) == ["failed"]
    r = status(sm, rid)
    assert r.error_code == "authorization_revoked" and r.attempts == 1


def test_storage_outage_retries(app, fake, sm, broker, worker, store, settings):
    rid = new_request(app, fake)
    store.fail_next_put = True
    assert drain(sm, broker, worker) == ["retry_scheduled"]
    reconcile(sm, settings.queue_name)
    assert drain(sm, broker, worker) == ["completed"]
    assert status(sm, rid).status == "completed"


def test_malformed_message_dead_lettered(broker, worker, settings):
    broker.publish(settings.queue_name, "m1", {"type": "evil", "cmd": "rm -rf /"})
    assert worker.handle(broker.get(settings.queue_name, 0)) == "dead_lettered"
    assert len(broker.dead) == 1


def test_drain_mode_exits_when_idle(worker):
    assert worker.run(drain=True, max_messages=None) == 0


def test_orphan_cleanup(app, fake, sm, broker, worker, store):
    rid = new_request(app, fake)
    drain(sm, broker, worker)
    store.put(f"statements/{rid}/old-dataset.pdf", b"x", __import__("hashlib").sha256(b"x").hexdigest())
    assert cleanup_orphans(sm, store, [rid]) == 1
    assert store.list(f"statements/{rid}/") == [status(sm, rid).result_key]
