"""Real RabbitMQ: topology, publisher confirms, manual ack, redelivery, dead-lettering, outages."""

from __future__ import annotations

import socket
import time
import uuid

import pytest

from nvb_platform.broker import DELIVERY_LIMIT, BrokerUnavailable, PublishError, RabbitBroker


@pytest.fixture
def broker(broker_settings, queues):
    b = RabbitBroker(broker_settings, declare=(queues["interactive"], queues["batch"]))
    yield b
    b.close()


def _msg():
    return str(uuid.uuid4()), {"type": "statement.generate", "request_id": str(uuid.uuid4()), "attempt": 0}


def test_queues_are_durable_quorum_with_dead_letter_and_delivery_limit(broker, broker_settings, queues):
    import pika

    q = queues["interactive"]
    # Re-declaring with the same arguments is a no-op (the relay does this on every connect) ...
    RabbitBroker(broker_settings, declare=(q,)).close()
    # ... and a declaration with different arguments is refused, which proves the stored arguments.
    other = RabbitBroker(broker_settings)
    with pytest.raises(pika.exceptions.ChannelClosedByBroker) as exc:
        other._ch.queue_declare(q, durable=True, arguments={
            "x-queue-type": "classic", "x-dead-letter-exchange": "nvb.dlx", "x-dead-letter-routing-key": q,
            "x-delivery-limit": DELIVERY_LIMIT})
    assert exc.value.reply_code == 406 and "x-queue-type" in exc.value.reply_text
    other.close()
    other = RabbitBroker(broker_settings)
    with pytest.raises(pika.exceptions.ChannelClosedByBroker) as exc:   # no dead-letter exchange
        other._ch.queue_declare(q, durable=True, arguments={"x-queue-type": "quorum"})
    assert exc.value.reply_code == 406 and "x-dead-letter-exchange" in exc.value.reply_text
    other.close()
    other = RabbitBroker(broker_settings)
    with pytest.raises(pika.exceptions.ChannelClosedByBroker) as exc:
        other._ch.queue_declare(q, durable=True, arguments={
            "x-queue-type": "quorum", "x-dead-letter-exchange": "nvb.dlx", "x-dead-letter-routing-key": q,
            "x-delivery-limit": DELIVERY_LIMIT + 1})
    assert exc.value.reply_code == 406 and "x-delivery-limit" in exc.value.reply_text
    other.close()


def test_confirmed_publish_get_ack(broker, queues):
    q = queues["interactive"]
    mid, body = _msg()
    broker.publish(q, mid, body)                       # returns only after the broker confirmed
    assert broker.depth(q) == 1
    d = broker.get(q, 2)
    assert d.message_id == mid and d.body == body and d.redelivered is False and d.queue == q
    broker.ack(d)
    assert broker.get(q, 0.3) is None and broker.depth(q) == 0


def test_publish_to_a_missing_queue_is_an_error_not_a_silent_drop(broker):
    mid, body = _msg()
    with pytest.raises(PublishError):
        broker.publish("nvb.test.does-not-exist", mid, body)   # mandatory + confirms -> unroutable


def test_unacked_message_is_redelivered_when_the_consumer_connection_dies(broker, broker_settings, queues):
    q = queues["interactive"]
    mid, body = _msg()
    broker.publish(q, mid, body)
    consumer = RabbitBroker(broker_settings, verify=(q,))
    d = consumer.get(q, 2)
    assert d is not None
    consumer._conn._impl._transport._sock.close() if hasattr(consumer._conn._impl, "_transport") else consumer.close()
    consumer.close()                                   # no ack was sent
    second = RabbitBroker(broker_settings, verify=(q,))
    again = None
    deadline = time.monotonic() + 15
    while again is None and time.monotonic() < deadline:
        again = second.get(q, 1)
    assert again is not None and again.message_id == mid and again.redelivered is True
    second.ack(again)
    second.close()


def test_reject_without_requeue_goes_to_the_dead_letter_queue(broker, queues):
    q = queues["interactive"]
    mid, body = _msg()
    broker.publish(q, mid, body)
    broker.reject(broker.get(q, 2), requeue=False)
    dead = None
    deadline = time.monotonic() + 10
    while dead is None and time.monotonic() < deadline:
        dead = broker.get(f"{q}.dead", 1)
    assert dead is not None and dead.message_id == mid
    broker.ack(dead)
    assert broker.depth(q) == 0


def test_poison_message_is_dead_lettered_after_the_delivery_limit(broker, queues):
    """A message that is requeued forever does not loop: the broker dead-letters it."""
    q = queues["batch"]
    mid, body = _msg()
    broker.publish(q, mid, body)
    deliveries = 0
    while True:
        d = broker.get(q, 3)
        if d is None:
            break
        deliveries += 1
        broker.reject(d, requeue=True)
        assert deliveries <= DELIVERY_LIMIT + 2
    assert deliveries == DELIVERY_LIMIT + 1
    dead = broker.get(f"{q}.dead", 5)
    assert dead is not None and dead.message_id == mid
    broker.ack(dead)


def test_worker_fails_fast_when_queues_are_missing(broker_settings):
    with pytest.raises(BrokerUnavailable):
        RabbitBroker(broker_settings, verify=("nvb.test.never-declared",))


def test_unreachable_broker_and_bad_credentials_fail_closed(broker_settings, tmp_path):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    class Down:
        broker_url = f"amqp://127.0.0.1:{port}/nvb-test"
        broker_username_file = broker_settings.broker_username_file
        broker_password_file = broker_settings.broker_password_file
        broker_ca_file = None

    with pytest.raises(BrokerUnavailable):
        RabbitBroker(Down())

    wrong = tmp_path / "pw"
    wrong.write_text("definitely-not-the-password", encoding="utf-8")

    class BadPassword:
        broker_url = broker_settings.broker_url
        broker_username_file = broker_settings.broker_username_file
        broker_password_file = str(wrong)
        broker_ca_file = None

    with pytest.raises(BrokerUnavailable):
        RabbitBroker(BadPassword())

    class Embedded:
        broker_url = "amqp://user:pw@127.0.0.1:5672/x"
        broker_username_file = broker_password_file = broker_ca_file = None

    with pytest.raises(BrokerUnavailable, match="must not embed credentials"):
        RabbitBroker(Embedded())


def test_outbox_survives_a_lost_connection_and_is_published_after_reconnect(stm_sm, stm_settings):
    """Broker 'outage' in the middle of relaying: rows stay unpublished, nothing is lost,
    and a fresh connection publishes them (at-least-once)."""
    from sqlalchemy import func, select

    from nvb_statements.models import OutboxMessage
    from nvb_statements.period import month_period
    from nvb_statements.relay import publish_pending
    from nvb_statements.service import create_request

    for i in range(5):
        with stm_sm() as db:
            create_request(db, owner_subject="nvb-user:1:1700000000", requested_by="nvb-user:1:1700000000",
                           account={"account_id": 500 + i, "masked_number": "•••• 0500"},
                           period=month_period("2026-09", "Africa/Cairo"), idempotency_key=uuid.uuid4().hex,
                           queue=stm_settings.queue_name)
    b = RabbitBroker(stm_settings, declare=(stm_settings.queue_name, stm_settings.batch_queue_name))
    b._conn.close()                                               # the connection is gone
    with pytest.raises(PublishError):
        publish_pending(stm_sm, b, 10, raise_on_failure=True)
    with stm_sm() as db:
        rows = db.scalars(select(OutboxMessage)).all()
        assert all(r.published_at is None for r in rows)
        assert sum(r.publish_attempts for r in rows) == 1 and any(r.last_error for r in rows)
    fresh = RabbitBroker(stm_settings, declare=(stm_settings.queue_name,))
    assert publish_pending(stm_sm, fresh, 10, raise_on_failure=True) == 5
    assert fresh.depth(stm_settings.queue_name) == 5
    with stm_sm() as db:
        assert db.scalar(select(func.count()).select_from(OutboxMessage)
                         .where(OutboxMessage.published_at.is_(None))) == 0
    fresh.close()


def test_relay_crash_after_confirm_republishes_a_duplicate_not_a_loss(stm_sm, stm_settings):
    from nvb_statements.period import month_period
    from nvb_statements.relay import publish_pending
    from nvb_statements.service import create_request

    with stm_sm() as db:
        create_request(db, owner_subject="nvb-user:1:1700000000", requested_by="nvb-user:1:1700000000",
                       account={"account_id": 7, "masked_number": "•••• 0007"},
                       period=month_period("2026-09", "Africa/Cairo"), idempotency_key=uuid.uuid4().hex,
                       queue=stm_settings.queue_name)
    b = RabbitBroker(stm_settings, declare=(stm_settings.queue_name,))
    with pytest.raises(RuntimeError):
        publish_pending(stm_sm, b, 10, crash_after_publish=True)   # confirmed, DB mark not committed
    assert publish_pending(stm_sm, b, 10) == 1
    first, second = b.get(stm_settings.queue_name, 2), b.get(stm_settings.queue_name, 2)
    assert first.message_id == second.message_id                    # duplicate delivery, same logical task
    b.ack(first), b.ack(second)
    b.close()


def test_keepalive_services_heartbeats_during_a_long_task(broker_settings, queues):
    """With a 4 s heartbeat RabbitMQ drops a silent connection after ~8 s. A consumer that calls
    keepalive() while working keeps its delivery and can still ack it."""

    class Short:
        broker_url = broker_settings.broker_url
        broker_username_file = broker_settings.broker_username_file
        broker_password_file = broker_settings.broker_password_file
        broker_ca_file = None
        broker_heartbeat_seconds = 4

    q = queues["interactive"]
    b = RabbitBroker(Short(), declare=(q,))
    mid, body = _msg()
    b.publish(q, mid, body)
    d = b.get(q, 2)
    end = time.monotonic() + 13
    while time.monotonic() < end:           # "long task" with periodic keepalive
        time.sleep(1)
        b.keepalive()
    b.ack(d)                                # the connection is still alive
    assert b.depth(q) == 0
    b.close()

    silent = RabbitBroker(Short(), verify=(q,))
    silent.publish(q, *_msg())
    d = silent.get(q, 2)
    time.sleep(13)                          # blocking work without keepalive: the broker gives up on us
    with pytest.raises(Exception):  # noqa: B017,PT011 - any pika connection error
        silent.ack(d)
        silent.keepalive()
        silent.depth(q)
    silent.close()
    check = RabbitBroker(broker_settings, verify=(q,))
    again = check.get(q, 5)
    assert again is not None and again.redelivered is True      # not lost: redelivered to the next consumer
    check.ack(again)
    check.close()
