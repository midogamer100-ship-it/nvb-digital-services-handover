"""Message broker abstraction (RabbitMQ in deployment, in-memory double in tests).

Semantics relied on by the services:
* publish() returns only after the broker confirmed the persistent message
  (publisher confirms, mandatory routing); otherwise it raises PublishError.
* get() hands out one message without auto-ack; the consumer acks only after the
  durable DB state is committed (at-least-once delivery, duplicates possible).
* reject(requeue=False) routes the message to the dead-letter queue.
"""

from __future__ import annotations

import json
import ssl
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from nvb_statements.config import Settings, read_secret


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


class Broker(Protocol):
    def publish(self, queue: str, message_id: str, body: dict) -> None: ...
    def get(self, queue: str, timeout: float) -> Delivery | None: ...
    def ack(self, delivery: Delivery) -> None: ...
    def reject(self, delivery: Delivery, requeue: bool) -> None: ...
    def depth(self, queue: str) -> int: ...
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
                    d = Delivery(mid, body, self._tag, redelivered)
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

    def close(self) -> None:
        pass


class RabbitBroker:
    """pika BlockingConnection with publisher confirms. One instance per process/thread."""

    def __init__(self, settings: Settings) -> None:
        import pika

        if not settings.broker_url:
            raise BrokerUnavailable("STATEMENTS_BROKER_URL is not configured")
        parts = urlsplit(settings.broker_url)
        creds = pika.PlainCredentials(read_secret(settings.broker_username_file) or "",
                                      read_secret(settings.broker_password_file) or "")
        ssl_options = None
        if parts.scheme == "amqps":
            ctx = ssl.create_default_context(cafile=settings.broker_ca_file) if settings.broker_ca_file \
                else ssl.create_default_context()
            ssl_options = pika.SSLOptions(ctx, server_hostname=parts.hostname)
        params = pika.ConnectionParameters(
            host=parts.hostname, port=parts.port or (5671 if ssl_options else 5672),
            virtual_host=(parts.path.lstrip("/") or "/"), credentials=creds, ssl_options=ssl_options,
            heartbeat=30, blocked_connection_timeout=30, connection_attempts=3, retry_delay=2)
        try:
            self._conn = pika.BlockingConnection(params)
        except pika.exceptions.AMQPError as exc:
            raise BrokerUnavailable(type(exc).__name__) from None
        self._ch = self._conn.channel()
        self._ch.confirm_delivery()
        self._pika = pika

    def publish(self, queue: str, message_id: str, body: dict) -> None:
        props = self._pika.BasicProperties(delivery_mode=2, message_id=message_id,
                                           content_type="application/json")
        try:
            self._ch.basic_publish("", queue, json.dumps(body).encode(), properties=props, mandatory=True)
        except (self._pika.exceptions.UnroutableError, self._pika.exceptions.NackError) as exc:
            raise PublishError(type(exc).__name__) from None
        except self._pika.exceptions.AMQPError as exc:
            raise PublishError(type(exc).__name__) from None

    def get(self, queue: str, timeout: float) -> Delivery | None:
        deadline = time.monotonic() + timeout
        while True:
            method, props, raw = self._ch.basic_get(queue, auto_ack=False)
            if method is not None:
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = {"_malformed": True}
                return Delivery(props.message_id or "", body, method.delivery_tag, method.redelivered)
            if time.monotonic() >= deadline:
                return None
            self._conn.sleep(0.5)

    def ack(self, delivery: Delivery) -> None:
        self._ch.basic_ack(delivery.tag)

    def reject(self, delivery: Delivery, requeue: bool) -> None:
        self._ch.basic_reject(delivery.tag, requeue=requeue)

    def depth(self, queue: str) -> int:
        return self._ch.queue_declare(queue, passive=True).method.message_count

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass


def declare_topology(broker: RabbitBroker, queue: str) -> None:
    """One-off topology setup (run by an operator Job, or replaced by infra-managed definitions)."""
    ch = broker._ch
    ch.exchange_declare("nvb.dlx", exchange_type="direct", durable=True)
    ch.queue_declare(f"{queue}.dead", durable=True, arguments={"x-queue-type": "quorum"})
    ch.queue_bind(f"{queue}.dead", "nvb.dlx", routing_key=queue)
    ch.queue_declare(queue, durable=True, arguments={
        "x-queue-type": "quorum", "x-dead-letter-exchange": "nvb.dlx", "x-dead-letter-routing-key": queue,
        "x-delivery-limit": 20})
