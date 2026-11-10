"""Transactional outbox publishing (shared by the statements and loans relays).

A service writes its business row and an outbox row in ONE database transaction. The relay
publishes unpublished rows with broker confirms and then marks them published. A crash after
the broker confirmed but before the mark commits produces a duplicate message, which consumers
tolerate (idempotent handlers). A broker outage leaves rows unpublished: nothing is lost.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from nvb_platform.broker import Broker, PublishError
from nvb_platform.db import utcnow

log = logging.getLogger("nvb.outbox")


def publish_pending(sm: sessionmaker[Session], broker: Broker, model, batch: int, *,
                    crash_after_publish: bool = False, on_published=None, on_failure=None) -> int:
    """Publish up to ``batch`` rows; returns how many were published.

    Rows are locked with FOR UPDATE SKIP LOCKED on PostgreSQL, so several relay replicas never
    publish the same row concurrently. On a publish failure the rows already confirmed in this
    call are committed as published, the failing row keeps ``published_at`` NULL (attempt counter
    and sanitized error recorded) and the call stops: the next cycle retries it.
    """
    published = 0
    with sm() as db:
        rows = db.scalars(select(model).where(model.published_at.is_(None))
                          .order_by(model.id).limit(batch).with_for_update(skip_locked=True)).all()
        for row in rows:
            try:
                broker.publish(row.queue, row.message_id, row.payload)
            except PublishError as exc:
                row.publish_attempts += 1
                row.last_error = str(exc)[:200]
                if on_failure:
                    on_failure()
                log.warning("publish failed; outbox row retained", extra={"outbox_id": row.id})
                break
            if crash_after_publish:
                raise RuntimeError("simulated relay crash after publish")
            row.published_at = utcnow()
            row.publish_attempts += 1
            published += 1
            if on_published:
                on_published()
        db.commit()
    return published


def backlog(sm: sessionmaker[Session], model) -> int:
    with sm() as db:
        return db.scalar(select(func.count()).select_from(model).where(model.published_at.is_(None))) or 0
