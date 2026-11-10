"""HTTP client for the core integration API (/internal/v1).

Protections for the core:
* bounded concurrency (asyncio semaphore with acquire timeout) per replica;
* token-bucket rate limit per replica;
* bounded connection pool and explicit connect/read timeouts;
* retries only for read-only calls, only on transient failures, with capped
  exponential backoff + jitter, honouring Retry-After;
* never logs tokens, cookies or response bodies.
"""

from __future__ import annotations

import asyncio
import logging
import random
import ssl
import time
from dataclasses import dataclass
from typing import Any

import httpx

from core_adapter.config import Settings
from core_adapter.credentials import SecretFile
from core_adapter.metrics import CORE_DURATION, CORE_INFLIGHT, CORE_REQUESTS, CORE_REJECTED

log = logging.getLogger("adapter.core")
TRANSIENT_STATUS = {429, 502, 503, 504}


class AdapterError(Exception):
    def __init__(self, status_code: int, code: str, message: str, *, retryable: bool = False,
                 retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after = retry_after


class TokenBucket:
    def __init__(self, rate: float, burst: int) -> None:
        self.rate, self.capacity = rate, float(burst)
        self.tokens, self.updated = float(burst), time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            async with self._lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return True
                wait = (1 - self.tokens) / self.rate
            if time.monotonic() + wait > deadline:
                return False
            await asyncio.sleep(wait)


def build_ssl_context(settings: Settings) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=settings.core_ca_file) if settings.core_ca_file \
        else ssl.create_default_context()
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


@dataclass
class CoreResponse:
    status_code: int
    body: Any


# Core error code -> (adapter status, adapter code, retryable)
CORE_ERROR_MAP = {
    "account_not_found": (404, "not_found", False),
    "subject_not_found": (404, "not_found", False),
    "invalid_subject": (422, "invalid_subject", False),
    "period_not_settled": (409, "period_not_settled", True),
    "invalid_period": (422, "invalid_period", False),
    "period_too_long": (422, "period_too_long", False),
    "naive_datetime": (422, "invalid_period", False),
    "invalid_cursor": (422, "invalid_cursor", False),
    "ledger_reconciliation_failed": (502, "core_integrity_error", False),
}


class CoreClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self.token = SecretFile(settings.core_token_file)
        self._sem = asyncio.Semaphore(settings.core_max_concurrency)
        self._bucket = TokenBucket(settings.core_rate_per_second, settings.core_rate_burst)
        limits = httpx.Limits(max_connections=settings.core_max_connections,
                              max_keepalive_connections=settings.core_max_connections)
        timeout = httpx.Timeout(settings.core_read_timeout_seconds, connect=settings.core_connect_timeout_seconds)
        kwargs: dict[str, Any] = {"base_url": settings.core_base_url or "http://core.invalid", "limits": limits,
                                  "timeout": timeout, "follow_redirects": False}
        if transport is not None:
            kwargs["transport"] = transport
        elif (settings.core_base_url or "").startswith("https://"):
            kwargs["verify"] = build_ssl_context(settings)
        self._http = httpx.AsyncClient(**kwargs)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _send_once(self, method: str, path: str, endpoint: str, request_id: str, **kw) -> httpx.Response:
        token = self.token.get()
        if not token:
            raise AdapterError(503, "core_credentials_missing", "Core credentials are not configured.")
        q = self.settings.core_queue_timeout_seconds
        if not await self._bucket.acquire(q):
            CORE_REJECTED.labels("rate_limited").inc()
            raise AdapterError(503, "adapter_busy", "Core call rate limit reached; retry later.",
                               retryable=True, retry_after=1)
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=q if q > 0 else 0.001)
        except TimeoutError:
            CORE_REJECTED.labels("concurrency").inc()
            raise AdapterError(503, "adapter_busy", "Core concurrency limit reached; retry later.",
                               retryable=True, retry_after=1) from None
        CORE_INFLIGHT.inc()
        started = time.perf_counter()
        outcome = "error"
        try:
            resp = await self._http.request(
                method, path, headers={"Authorization": f"Bearer {token}", "X-Request-ID": request_id,
                                       "Accept": "application/json"}, **kw)
            outcome = f"{resp.status_code // 100}xx"
            return resp
        except httpx.TimeoutException:
            outcome = "timeout"
            raise
        finally:
            CORE_INFLIGHT.dec()
            self._sem.release()
            CORE_DURATION.labels(endpoint).observe(time.perf_counter() - started)
            CORE_REQUESTS.labels(endpoint, outcome).inc()

    async def call(self, method: str, path: str, *, endpoint: str, request_id: str, retry: bool,
                   **kw) -> CoreResponse:
        attempts = self.settings.core_retry_attempts if retry else 1
        last: AdapterError | None = None
        for attempt in range(attempts):
            try:
                resp = await self._send_once(method, path, endpoint, request_id, **kw)
            except httpx.TimeoutException:
                last = AdapterError(503, "core_timeout", "The core did not respond in time.", retryable=True)
            except httpx.TransportError:
                last = AdapterError(503, "core_unavailable", "The core is unreachable.", retryable=True)
            except AdapterError as exc:
                if not exc.retryable:
                    raise
                last = exc
            else:
                if resp.status_code < 400:
                    try:
                        return CoreResponse(resp.status_code, resp.json())
                    except ValueError:
                        raise AdapterError(502, "core_contract_violation", "Core returned invalid JSON.") from None
                last = self._map_error(resp)
                if not (last.retryable and resp.status_code in TRANSIENT_STATUS):
                    raise last
            if attempt + 1 < attempts:
                delay = min(self.settings.core_retry_max_seconds,
                            self.settings.core_retry_base_seconds * (2 ** attempt))
                if last.retry_after:
                    delay = min(self.settings.core_retry_max_seconds, max(delay, last.retry_after))
                await asyncio.sleep(delay * (0.5 + random.random() / 2))
        assert last is not None
        log.warning("core call failed", extra={"endpoint": endpoint, "code": last.code, "attempts": attempts})
        raise last

    @staticmethod
    def _map_error(resp: httpx.Response) -> AdapterError:
        code = None
        try:
            code = resp.json().get("error", {}).get("code")
        except (ValueError, AttributeError):
            pass
        retry_after = None
        try:
            retry_after = float(resp.headers.get("Retry-After", ""))
        except ValueError:
            pass
        if resp.status_code in (401, 403):
            # The adapter's own credentials/scopes are wrong: an operator problem, not the caller's.
            log.error("core rejected adapter credentials", extra={"status": resp.status_code, "code": code})
            return AdapterError(502, "core_auth_misconfigured", "The adapter is not authorised by the core.")
        if resp.status_code == 429:
            return AdapterError(503, "core_busy", "The core is busy; retry later.", retryable=True,
                                retry_after=retry_after or 1)
        if resp.status_code in (502, 503, 504):
            return AdapterError(503, "core_unavailable", "The core is unavailable.", retryable=True,
                                retry_after=retry_after)
        if code in CORE_ERROR_MAP:
            status, mapped, retryable = CORE_ERROR_MAP[code]
            return AdapterError(status, mapped, resp.json()["error"].get("message", mapped), retryable=retryable)
        if resp.status_code == 422:
            return AdapterError(422, "invalid_request", "The core rejected the request parameters.")
        if resp.status_code == 404:
            return AdapterError(404, "not_found", "Not found.")
        return AdapterError(502, "core_error", "Unexpected core response.")
