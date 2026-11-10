"""statements-relay: transactional-outbox publisher and task reconciler.

Publishing: unpublished outbox rows are locked (FOR UPDATE SKIP LOCKED on PostgreSQL, so several
relays can run), published with broker confirms, then marked published. A crash after the broker
confirmed but before the mark commits produces a duplicate message, which the worker tolerates.
A broker outage leaves rows unpublished (attempt counter + sanitized error) - nothing is lost.

Reconciliation (same process, every cycle):
* running requests whose lease expired (worker died)   -> queued + new outbox message
* queued requests whose retry backoff (not_before) is due -> new outbox message, not_before cleared
* queued requests untouched for ``stale_seconds`` (message lost/dead-lettered) -> new outbox message

Run: ``python -m nvb_statements.relay`` (long-running Deployment, 1 replica is enough) or ``--once``.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import sys
import time
from datetime import timedelta

from prometheus_client import Counter, Gauge
from sqlalchemy import and_, select, update
from sqlalchemy.orm import Session, sessionmaker

from nvb_statements.broker import Broker, PublishError
from nvb_statements.config import Settings, get_settings
from nvb_statements.db import build_engine, build_sessionmaker
from nvb_statements.logging_setup import configure_logging
from nvb_statements.models import OutboxMessage, StatementRequest, utcnow
from nvb_statements.service import enqueue, record_event

log = logging.getLogger("statements.relay")
PUBLISHED = Counter("statements_outbox_published_total", "Outbox messages published")
PUBLISH_FAIL = Counter("statements_outbox_publish_failures_total", "Outbox publish failures")
BACKLOG = Gauge("statements_outbox_backlog", "Unpublished outbox messages")
RECOVERED = Counter("statements_reconciler_requeued_total", "Requests re-enqueued", ["reason"])


def publish_pending(sm: sessionmaker[Session], broker: Broker, batch: int, crash_after_publish: bool = False) -> int:
    published = 0
    with sm() as db:
        rows = db.scalars(select(OutboxMessage).where(OutboxMessage.published_at.is_(None))
                          .order_by(OutboxMessage.id).limit(batch).with_for_update(skip_locked=True)).all()
        for row in rows:
            try:
                broker.publish(row.queue, row.message_id, row.payload)
            except PublishError as exc:
                row.publish_attempts += 1
                row.last_error = str(exc)[:200]
                PUBLISH_FAIL.inc()
                log.warning("publish failed; will retry", extra={"outbox_id": row.id})
                break
            if crash_after_publish:
                raise RuntimeError("simulated relay crash after publish")
            row.published_at = utcnow()
            row.publish_attempts += 1
            published += 1
            PUBLISHED.inc()
        db.commit()
        BACKLOG.set(db.query(OutboxMessage).filter(OutboxMessage.published_at.is_(None)).count())
    return published


def reconcile(sm: sessionmaker[Session], queue: str, stale_seconds: int = 900, limit: int = 200) -> int:
    now = utcnow()
    n = 0
    with sm() as db:
        expired = db.scalars(select(StatementRequest).where(StatementRequest.status == "running",
                                                            StatementRequest.lease_expires_at < now)
                             .limit(limit).with_for_update(skip_locked=True)).all()
        for r in expired:
            r.status, r.lease_owner, r.lease_expires_at, r.updated_at = "queued", None, None, now
            enqueue(db, r.id, queue, r.attempts)
            record_event(db, r.id, "lease_expired_requeued", "relay", attempts=r.attempts)
            RECOVERED.labels("lease_expired").inc()
            n += 1
        due = db.scalars(select(StatementRequest).where(StatementRequest.status == "queued",
                                                        StatementRequest.not_before.is_not(None),
                                                        StatementRequest.not_before <= now)
                         .limit(limit).with_for_update(skip_locked=True)).all()
        for r in due:
            r.not_before, r.updated_at = None, now
            enqueue(db, r.id, queue, r.attempts)
            RECOVERED.labels("retry_due").inc()
            n += 1
        stale = db.scalars(select(StatementRequest).where(
            and_(StatementRequest.status == "queued", StatementRequest.not_before.is_(None),
                 StatementRequest.updated_at < now - timedelta(seconds=stale_seconds)))
            .limit(limit).with_for_update(skip_locked=True)).all()
        for r in stale:
            r.updated_at = now
            enqueue(db, r.id, queue, r.attempts)
            RECOVERED.labels("stale").inc()
            n += 1
        db.commit()
    return n


def cleanup_orphans(sm: sessionmaker[Session], store, request_ids: list[str]) -> int:
    """Delete objects under a completed request's prefix that are not its recorded result."""
    removed = 0
    with sm() as db:
        for rid in request_ids:
            r = db.get(StatementRequest, rid)
            if r is None or r.status != "completed":
                continue
            for key in store.list(f"statements/{rid}/"):
                if key != r.result_key:
                    store.delete(key)
                    removed += 1
    return removed


def main(argv: list[str] | None = None) -> int:
    from nvb_statements.broker import BrokerUnavailable, RabbitBroker

    p = argparse.ArgumentParser(prog="statements-relay")
    p.add_argument("--once", action="store_true")
    args = p.parse_args(argv)
    s: Settings = get_settings()
    configure_logging(s.log_level, "statements-relay", f"{socket.gethostname()}:{os.getpid()}")
    sm = build_sessionmaker(build_engine(s))
    running = {"on": True}
    signal.signal(signal.SIGTERM, lambda *_: running.update(on=False))
    broker = None
    while running["on"]:
        try:
            broker = broker or RabbitBroker(s)
            publish_pending(sm, broker, s.relay_batch_size)
        except (BrokerUnavailable, PublishError):
            log.warning("broker unavailable; outbox retained")
            broker = None
        reconcile(sm, s.queue_name)
        if args.once:
            break
        time.sleep(s.relay_poll_seconds)
    if broker:
        broker.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
