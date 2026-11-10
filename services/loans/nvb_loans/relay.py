"""loans-relay: outbox publisher (declares the loans queue), task reconciler, batch finalizer
and the scheduled/manual batch command.

    python -m nvb_loans.relay                      # long-running Deployment
    python -m nvb_loans.relay --once
    python -m nvb_loans.relay batch-create --trigger schedule [--idempotency-key K]
        (same ``create_summary_batch`` operation as POST /x/loans/v1/batches; run by a CronJob)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import sys
import time
from datetime import timedelta
from pathlib import Path

from sqlalchemy import and_, select
from sqlalchemy.orm import Session, sessionmaker

from nvb_loans import service as svc
from nvb_loans.config import Settings, get_settings
from nvb_loans.db import build_engine, build_sessionmaker
from nvb_loans.models import LoanBatch, OutboxMessage, SummaryTask, utcnow
from nvb_platform import outbox
from nvb_platform.broker import Broker, PublishError
from nvb_platform.logging_setup import configure_logging

log = logging.getLogger("loans.relay")


def publish_pending(sm: sessionmaker[Session], broker: Broker, batch: int, crash_after_publish: bool = False,
                    raise_on_failure: bool = False) -> int:
    failures: list[int] = []
    n = outbox.publish_pending(sm, broker, OutboxMessage, batch, crash_after_publish=crash_after_publish,
                               on_failure=lambda: failures.append(1))
    if failures and raise_on_failure:
        raise PublishError("publish failed; the connection will be re-established")
    return n


def reconcile(sm: sessionmaker[Session], queue: str, stale_seconds: int = 900, limit: int = 200) -> int:
    now, n = utcnow(), 0
    with sm() as db:
        expired = db.scalars(select(SummaryTask).where(SummaryTask.status == "running",
                                                       SummaryTask.lease_expires_at < now)
                             .limit(limit).with_for_update(skip_locked=True)).all()
        for t in expired:
            t.status, t.lease_owner, t.lease_expires_at, t.updated_at = "queued", None, None, now
        due = db.scalars(select(SummaryTask).where(SummaryTask.status == "queued", SummaryTask.not_before.is_not(None),
                                                   SummaryTask.not_before <= now)
                         .limit(limit).with_for_update(skip_locked=True)).all()
        for t in due:
            t.not_before, t.updated_at = None, now
        stale = db.scalars(select(SummaryTask).where(
            and_(SummaryTask.status == "queued", SummaryTask.not_before.is_(None),
                 SummaryTask.updated_at < now - timedelta(seconds=stale_seconds)))
            .limit(limit).with_for_update(skip_locked=True)).all()
        for t in stale:
            t.updated_at = now
        for t in {t.id: t for t in [*expired, *due, *stale]}.values():
            svc.publish_task(db, t.id, queue)
            n += 1
        db.commit()
        for b in db.scalars(select(LoanBatch).where(LoanBatch.status == "running").limit(50)).all():
            svc.finalize_batch(db, b)
    return n


def main(argv: list[str] | None = None) -> int:
    from nvb_platform.broker import BrokerUnavailable, RabbitBroker

    p = argparse.ArgumentParser(prog="loans-relay")
    p.add_argument("command", nargs="?", choices=["run", "batch-create"], default="run")
    p.add_argument("--once", action="store_true")
    p.add_argument("--heartbeat-file", default=os.environ.get("LOANS_RELAY_HEARTBEAT_FILE"))
    p.add_argument("--trigger", choices=["manual", "schedule"], default="schedule")
    p.add_argument("--actor", default="system:schedule")
    p.add_argument("--idempotency-key")
    args = p.parse_args(argv)
    s: Settings = get_settings()
    configure_logging(s.log_level, "loans-relay", f"{socket.gethostname()}:{os.getpid()}")
    problems = s.validate_runtime("relay" if args.command == "run" else "batch")
    if problems:
        log.error("invalid configuration", extra={"problems": problems})
        return 78
    sm = build_sessionmaker(build_engine(s, "nvb-loans-relay"))
    if args.command == "batch-create":
        key = args.idempotency_key or f"{args.trigger}-{utcnow():%Y-%m-%d}"   # one scheduled batch per day
        try:
            with sm() as db:
                b, created = svc.create_summary_batch(db, queue=s.queue_name, created_by=args.actor,
                                                      trigger=args.trigger, idempotency_key=key,
                                                      max_applications=s.batch_max_applications)
                print(json.dumps({"id": b.id, "created": created, "status": b.status, "total": b.total_items}))
        except svc.ServiceError as exc:
            log.error("batch rejected", extra={"code": exc.code})
            return 2
        return 0

    running = {"on": True}
    signal.signal(signal.SIGTERM, lambda *_: running.update(on=False))
    signal.signal(signal.SIGINT, lambda *_: running.update(on=False))
    broker, rc = None, 0
    while running["on"]:
        try:
            if broker is None:
                broker = RabbitBroker(s, declare=(s.queue_name,))
                log.info("broker connected; queue declared")
        except BrokerUnavailable as exc:
            log.warning("broker unavailable; outbox retained", extra={"error": str(exc)})
            broker, rc = None, 75
        try:
            try:
                if broker is not None:
                    publish_pending(sm, broker, s.relay_batch_size, raise_on_failure=True)
                    rc = 0
            finally:
                reconcile(sm, s.queue_name, stale_seconds=s.stale_requeue_seconds)
            if args.heartbeat_file:
                Path(args.heartbeat_file).write_text(str(int(time.time())), encoding="utf-8")
        except PublishError:
            if broker is not None:
                broker.close()
            broker = None
        except Exception:  # noqa: BLE001
            log.exception("relay cycle failed; retrying")
            if broker is not None:
                broker.close()
            broker, rc = None, 1
        if args.once:
            break
        time.sleep(s.relay_poll_seconds)
    if broker:
        broker.close()
    return rc if args.once else 0


if __name__ == "__main__":
    sys.exit(main())
