"""Message broker abstraction (RabbitMQ in deployment, in-memory double in unit tests).

Semantics relied on by the services:
* publish() returns only after the broker confirmed the persistent message
  (publisher confirms, mandatory routing); otherwise it raises PublishError.
* get() hands out one message without auto-ack; the consumer acks only after the
  durable DB state is committed (at-least-once delivery, duplicates possible).
* reject(requeue=False) routes the message to the dead-letter queue.
* Queues are durable quorum queues with a delivery limit: a message that keeps being
  requeued (poison) is dead-lettered by the broker instead of looping forever.

``settings`` is any object with ``broker_url``, ``broker_username_file``,
``broker_password_file``, ``broker_ca_file`` and optionally ``broker_heartbeat_seconds``.
The URL never embeds credentials; they are read from files (mounted Secrets).
"""

from __future__ import annotations

import json
import ssl
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import unquote, urlsplit

from nvb_platform.secrets import read_secret

DEAD_LETTER_EXCHANGE = "nvb.dlx"
DELIVERY_LIMIT = 20


class PublishError(Exception):
    pass


class BrokerUnavailable(Exception):
    pass


@dataclass
class Delivery:
    message_id: str
    body: dict
    tag: object
    redelivered: bool = False
    queue: str = ""


class Broker(Protocol):
    def publish(self, queue: str, message_id: str, body: dict) -> None: ...
    def get(self, queue: str, timeout: float) -> Delivery | None: ...
    def ack(self, delivery: Delivery) -> None: ...
    def reject(self, delivery: Delivery, requeue: bool) -> None: ...
    def depth(self, queue: str) -> int: ...
    def keepalive(self) -> None: ...
    def close(self) -> None: ...


class InMemoryBroker:
    def __init__(self) -> None:
        self.queues: dict[str, deque] = {}
        self.unacked: dict[int, tuple[str, Delivery]] = {}
        self.dead: list[Delivery] = []
        self.published: list[str] = []
        self.fail_publish = False
        self._tag = 0
        self._lock = threading.Lock()

    def publish(self, queue: str, message_id: str, body: dict) -> None:
        if self.fail_publish:
            raise PublishError("simulated broker outage")
        with self._lock:
            self.queues.setdefault(queue, deque()).append((message_id, json.loads(json.dumps(body)), False))
            self.published.append(message_id)

    def get(self, queue: str, timeout: float) -> Delivery | None:
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                q = self.queues.setdefault(queue, deque())
                if q:
                    mid, body, redelivered = q.popleft()
                    self._tag += 1
                    d = Delivery(mid, body, self._tag, redelivered, queue)
                    self.unacked[self._tag] = (queue, d)
                    return d
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.01)

    def ack(self, delivery: Delivery) -> None:
        with self._lock:
            self.unacked.pop(delivery.tag, None)

    def reject(self, delivery: Delivery, requeue: bool) -> None:
        with self._lock:
            queue, d = self.unacked.pop(delivery.tag)
            if requeue:
                self.queues[queue].appendleft((d.message_id, d.body, True))
            else:
                self.dead.append(d)

    def crash_consumer(self) -> None:
        """Simulate a consumer dying: every unacked message is redelivered."""
        with self._lock:
            for queue, d in self.unacked.values():
                self.queues[queue].appendleft((d.message_id, d.body, True))
            self.unacked.clear()

    def depth(self, queue: str) -> int:
        return len(self.queues.get(queue, ()))

    def keepalive(self) -> None:
        pass

    def close(self) -> None:
        pass


class RabbitBroker:
    """pika BlockingConnection with publisher confirms. One instance per process/thread.

    ``declare`` lists queues to declare (idempotently) on connect: the relay (publisher)
    passes its queues so that ``mandatory`` publishes are routable; workers pass
    ``verify`` to fail fast (passive declare) when a queue does not exist.
    """

    def __init__(self, settings, *, declare: tuple[str, ...] = (), verify: tuple[str, ...] = ()) -> None:
        import pika

        self._pika = pika
        if not settings.broker_url:
            raise BrokerUnavailable("broker URL is not configured")
        parts = urlsplit(settings.broker_url)
        if parts.username or parts.password:
            raise BrokerUnavailable("broker URL must not embed credentials; use the *_FILE settings")
        username = read_secret(settings.broker_username_file)
        password = read_secret(settings.broker_password_file)
        if not username or not password:
            raise BrokerUnavailable("broker credentials are not configured")
        ssl_options = None
        if parts.scheme == "amqps":
            ctx = ssl.create_default_context(cafile=settings.broker_ca_file) if settings.broker_ca_file \
                else ssl.create_default_context()
            ssl_options = pika.SSLOptions(ctx, server_hostname=parts.hostname)
        elif parts.scheme != "amqp":
            raise BrokerUnavailable("broker URL must be amqp:// or amqps://")
        heartbeat = int(getattr(settings, "broker_heartbeat_seconds", 600))
        params = pika.ConnectionParameters(
            host=parts.hostname, port=parts.port or (5671 if ssl_options else 5672),
            virtual_host=(unquote(parts.path.lstrip("/")) or "/"),
            credentials=pika.PlainCredentials(username, password), ssl_options=ssl_options,
            heartbeat=heartbeat, blocked_connection_timeout=30, connection_attempts=2, retry_delay=1,
            socket_timeout=10)
        try:
            self._conn = pika.BlockingConnection(params)
            self._ch = self._conn.channel()
            self._ch.confirm_delivery()
            for queue in declare:
                self.declare_queue(queue)
            for queue in verify:
                self._ch.queue_declare(queue, passive=True)
        except (pika.exceptions.AMQPError, OSError) as exc:
            raise BrokerUnavailable(type(exc).__name__) from None

    def declare_queue(self, queue: str) -> None:
        """Durable quorum queue + dead-letter queue. Re-declaring with equal arguments is a no-op."""
        ch = self._ch
        ch.exchange_declare(DEAD_LETTER_EXCHANGE, exchange_type="direct", durable=True)
        ch.queue_declare(f"{queue}.dead", durable=True, arguments={"x-queue-type": "quorum"})
        ch.queue_bind(f"{queue}.dead", DEAD_LETTER_EXCHANGE, routing_key=queue)
        ch.queue_declare(queue, durable=True, arguments={
            "x-queue-type": "quorum", "x-dead-letter-exchange": DEAD_LETTER_EXCHANGE,
            "x-dead-letter-routing-key": queue, "x-delivery-limit": DELIVERY_LIMIT})

    def publish(self, queue: str, message_id: str, body: dict) -> None:
        props = self._pika.BasicProperties(delivery_mode=2, message_id=message_id,
                                           content_type="application/json")
        try:
            self._ch.basic_publish("", queue, json.dumps(body).encode(), properties=props, mandatory=True)
        except (self._pika.exceptions.AMQPError, OSError) as exc:  # Unroutable, Nack, stream lost, ...
            raise PublishError(type(exc).__name__) from None

    def get(self, queue: str, timeout: float) -> Delivery | None:
        deadline = time.monotonic() + timeout
        while True:
            method, props, raw = self._ch.basic_get(queue, auto_ack=False)
            if method is not None:
                try:
                    body = json.loads(raw)
                    if not isinstance(body, dict):
                        body = {"_malformed": True}
                except ValueError:
                    body = {"_malformed": True}
                return Delivery(props.message_id or "", body, method.delivery_tag, method.redelivered, queue)
            if time.monotonic() >= deadline:
                return None
            self._conn.sleep(min(0.5, max(0.05, deadline - time.monotonic())))

    def ack(self, delivery: Delivery) -> None:
        self._ch.basic_ack(delivery.tag)

    def reject(self, delivery: Delivery, requeue: bool) -> None:
        self._ch.basic_reject(delivery.tag, requeue=requeue)

    def depth(self, queue: str) -> int:
        return self._ch.queue_declare(queue, passive=True).method.message_count

    def keepalive(self) -> None:
        """Service heartbeats during a long task (call from the thread that owns the connection)."""
        self._conn.process_data_events(time_limit=0)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass
