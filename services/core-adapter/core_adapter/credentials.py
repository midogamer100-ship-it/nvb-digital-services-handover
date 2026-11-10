"""Service credentials: inbound client registry and the outbound core token.

Both are files mounted from Secrets and re-read when they change, so rotation does not
need a restart. Only SHA-256 digests of inbound tokens are stored. Invalid or missing
configuration fails closed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger("adapter.credentials")

ADAPTER_SCOPES = {"identity:resolve", "accounts:read", "statements:read", "summaries:read", "discovery:read"}
MIN_TOKEN_LENGTH = 32
RELOAD_CHECK_SECONDS = 15.0


@dataclass(frozen=True)
class Client:
    client_id: str
    digests: tuple[str, ...]
    scopes: frozenset[str]


@dataclass
class _Watched:
    path: str | None
    mtime: float | None = None
    checked: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def changed(self) -> tuple[bool, float | None]:
        now = time.monotonic()
        if self.mtime is not None and now - self.checked < RELOAD_CHECK_SECONDS:
            return False, self.mtime
        self.checked = now
        try:
            mtime = os.stat(self.path).st_mtime if self.path else None
        except OSError:
            mtime = None
        return mtime != self.mtime or mtime is None, mtime


class ClientRegistry:
    def __init__(self, path: str | None) -> None:
        self._w = _Watched(path)
        self._clients: list[Client] = []
        self.valid = False

    def _reload(self) -> None:
        with self._w.lock:
            changed, mtime = self._w.changed()
            if not changed:
                return
            self._w.mtime = mtime
            if mtime is None:
                self._clients, self.valid = [], False
                return
            try:
                with open(self._w.path, encoding="utf-8") as fh:
                    raw = json.load(fh)
                clients = []
                for item in raw["clients"]:
                    scopes = frozenset(item["scopes"])
                    digests = tuple(d.lower() for d in item["token_sha256"])
                    if scopes - ADAPTER_SCOPES or not digests or any(len(d) != 64 for d in digests):
                        raise ValueError(f"invalid client entry {item.get('client_id')!r}")
                    clients.append(Client(str(item["client_id"]), digests, scopes))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                log.error("clients file invalid; failing closed", extra={"error": type(exc).__name__})
                self._clients, self.valid = [], False
                return
            self._clients, self.valid = clients, True

    def authenticate(self, token: str | None) -> Client | None:
        self._reload()
        if not token or not (MIN_TOKEN_LENGTH <= len(token) <= 512):
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        found = None
        for c in self._clients:
            for d in c.digests:
                if hmac.compare_digest(d, digest):
                    found = c
        return found

    def is_valid(self) -> bool:
        self._reload()
        return self.valid


class SecretFile:
    """A single secret value read from a file and refreshed on change."""

    def __init__(self, path: str | None) -> None:
        self._w = _Watched(path)
        self._value: str | None = None

    def get(self) -> str | None:
        with self._w.lock:
            changed, mtime = self._w.changed()
            if changed:
                self._w.mtime = mtime
                try:
                    value = open(self._w.path, encoding="utf-8").read().strip() if mtime is not None else ""
                except OSError:
                    value = ""
                self._value = value or None
        return self._value
