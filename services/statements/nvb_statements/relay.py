"""statements-relay: transactional-outbox publisher and task reconciler.

Publishing: unpublished outbox rows are locked (FOR UPDATE SKIP LOCKED on PostgreSQL, so several
relays can run), published with broker confirms, then marked published. A crash after the broker
confirmed but before the mark commits produces a duplicate message, which the worker tolerates.
A broker outage leaves rows unpublished (attempt counter + sanitized error) - nothing is lost.

The relay is also the component that DECLARES the queues (durable quorum queues with a
dead-letter queue and a delivery limit) every time it connects; workers only verify them.

Reconciliation (same process, every cycle):
* running requests whose lease expired (worker died)   -> queued + new outbox message
* queued requests whose retry backoff (not_before) is due -> new outbox message, not_before cleared
* queued requests untouched for ``stale_seconds`` (message lost/dead-lettered) -> new outbox message
* running batches whose members are all terminal -> completed / completed_with_failures

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
from pathlib import Path

from prometheus_client import Counter, Gauge
from sqlalchemy import and_, select
from sqlalchemy.orm import Session, sessionmaker

from nvb_platform import outbox
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


def publish_pending(sm: sessionmaker[Session], broker: Broker, batch: int, crash_after_publish: bool = False,
                    raise_on_failure: bool = False) -> int:
    failures: list[int] = []

    def failed() -> None:
        PUBLISH_FAIL.inc()
        failures.append(1)

    n = outbox.publish_pending(sm, broker, OutboxMessage, batch, crash_after_publish=crash_after_publish,
                               on_published=PUBLISHED.inc, on_failure=failed)
    BACKLOG.set(outbox.backlog(sm, OutboxMessage))
    if failures and raise_on_failure:
        raise PublishError("publish failed; the connection will be re-established")
    return n


def reconcile(sm: sessionmaker[Session], queue: str | None = None, stale_seconds: int = 900, limit: int = 200) -> int:
    """Re-enqueue recoverable requests on THEIR queue (``queue`` is kept for compatibility and
    only used for rows that predate the per-request queue column)."""
    now = utcnow()
    n = 0
    with sm() as db:
        expired = db.scalars(select(StatementRequest).where(StatementRequest.status == "running",
                                                            StatementRequest.lease_expires_at < now)
                             .limit(limit).with_for_update(skip_locked=True)).all()
        for r in expired:
            r.status, r.lease_owner, r.lease_expires_at, r.updated_at = "queued", None, None, now
            enqueue(db, r.id, r.queue or queue, r.attempts)
            record_event(db, r.id, "lease_expired_requeued", "relay", attempts=r.attempts)
            RECOVERED.labels("lease_expired").inc()
            n += 1
        due = db.scalars(select(StatementRequest).where(StatementRequest.status == "queued",
                                                        StatementRequest.not_before.is_not(None),
                                                        StatementRequest.not_before <= now)
                         .limit(limit).with_for_update(skip_locked=True)).all()
        for r in due:
            r.not_before, r.updated_at = None, now
            enqueue(db, r.id, r.queue or queue, r.attempts)
            RECOVERED.labels("retry_due").inc()
            n += 1
        stale = db.scalars(select(StatementRequest).where(
            and_(StatementRequest.status == "queued", StatementRequest.not_before.is_(None),
                 StatementRequest.updated_at < now - timedelta(seconds=stale_seconds)))
            .limit(limit).with_for_update(skip_locked=True)).all()
        for r in stale:
            r.updated_at = now
            enqueue(db, r.id, r.queue or queue, r.attempts)
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


def run_cycle(sm: sessionmaker[Session], broker: Broker | None, s: Settings) -> None:
    from nvb_statements.batch import finalize_running

    try:
        if broker is not None:
            publish_pending(sm, broker, s.relay_batch_size, raise_on_failure=True)
    finally:
        reconcile(sm, s.queue_name, stale_seconds=s.stale_requeue_seconds)
        finalize_running(sm)


def main(argv: list[str] | None = None) -> int:
    from nvb_statements.broker import BrokerUnavailable, RabbitBroker

    p = argparse.ArgumentParser(prog="statements-relay")
    p.add_argument("--once", action="store_true")
    p.add_argument("--heartbeat-file", default=os.environ.get("STATEMENTS_RELAY_HEARTBEAT_FILE"),
                   help="touched after every successful database cycle (used by the liveness probe)")
    args = p.parse_args(argv)
    s: Settings = get_settings()
    configure_logging(s.log_level, "statements-relay", f"{socket.gethostname()}:{os.getpid()}")
    problems = s.validate_runtime("relay")
    if problems:
        log.error("invalid configuration", extra={"problems": problems})
        return 78
    sm = build_sessionmaker(build_engine(s, "nvb-statements-relay"))
    running = {"on": True}
    signal.signal(signal.SIGTERM, lambda *_: running.update(on=False))
    signal.signal(signal.SIGINT, lambda *_: running.update(on=False))
    broker = None
    rc = 0
    while running["on"]:
        try:
            if broker is None:
                broker = RabbitBroker(s, declare=(s.queue_name, s.batch_queue_name))
                log.info("broker connected; queues declared")
        except BrokerUnavailable as exc:
            log.warning("broker unavailable; outbox retained", extra={"error": str(exc)})
            broker = None
            rc = 75
        try:
            run_cycle(sm, broker, s)
            rc = rc if broker is None else 0
            if args.heartbeat_file:
                Path(args.heartbeat_file).write_text(str(int(time.time())), encoding="utf-8")
        except PublishError:
            if broker is not None:
                broker.close()
            broker = None
        except Exception:  # noqa: BLE001 - database outage etc.: keep running, retry next cycle
            log.exception("relay cycle failed; retrying")
            if broker is not None:
                broker.close()
            broker = None
            rc = 1
        if args.once:
            break
        time.sleep(s.relay_poll_seconds)
    if broker:
        broker.close()
    return rc if args.once else 0


if __name__ == "__main__":
    sys.exit(main())
