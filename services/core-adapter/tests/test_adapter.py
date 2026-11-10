"""Core Adapter unit tests against a mocked core (httpx.MockTransport)."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from tests.conftest import adapter_client, auth, token

SUBJECT = "nvb-user:5:1700000000"
PERIOD = {"subject": SUBJECT, "period_start": "2026-09-01T00:00:00+03:00", "period_end": "2026-10-01T00:00:00+03:00"}
ACCOUNT = {"account_id": 7, "masked_number": "•••• 1234", "type": "current", "nickname": "Everyday",
           "currency": "EGP", "status": "active", "opened_at": "2026-01-01T09:00:00+00:00"}


def manifest_body(**over):
    body = {"dataset_version": "statement-v2", "dataset_id": "a" * 64, "entries_sha256": "b" * 64, "account_id": 7,
            "period_start": "2026-08-31T21:00:00+00:00", "period_end": "2026-09-30T21:00:00+00:00",
            "boundary": "[start,end) UTC", "ordering": "posted_at ASC, id ASC", "currency": "EGP",
            "opening_balance_minor": 1000, "closing_balance_minor": 1500, "credit_total_minor": 700,
            "debit_total_minor": 200, "entry_count": 3, "first_entry_id": 1, "last_entry_id": 3,
            "account": {**ACCOUNT, "holder_name": "Test Holder"}, "account_opened_in_period": False,
            "account_opened_after_period": False, "generated_at": "2026-11-10T00:00:00+00:00"}
    body.update(over)
    return body


# --- Inbound authentication -------------------------------------------------------------


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic x"}])
async def test_invalid_service_credentials_fail_closed(settings_factory, headers):
    calls = []
    async with adapter_client(settings_factory(), lambda r: calls.append(r) or httpx.Response(200, json={})) as c:
        resp = await c.post("/v1/identity/resolve", headers=headers, json={"session_token": "s"})
        assert resp.status_code == 401
        resp = await c.post("/v1/identity/resolve", headers=auth(token()), json={"session_token": "s"})
        assert resp.status_code == 401
    assert calls == []  # nothing reaches the core


async def test_scope_enforced(settings_factory, tokens):
    async with adapter_client(settings_factory(), lambda r: httpx.Response(200, json={})) as c:
        resp = await c.get("/v1/statement-datasets/7/manifest", headers=auth(tokens["identity"]), params=PERIOD)
        assert resp.status_code == 403
        resp = await c.get("/v1/discovery/statement-accounts", headers=auth(tokens["statements"]))
        assert resp.status_code == 403
        resp = await c.post("/v1/identity/resolve", headers=auth(tokens["identity"]),
                            json={"session_token": "s", "include_accounts": True})
        assert resp.status_code == 403


async def test_missing_clients_file_fails_closed(settings_factory, tokens):
    async with adapter_client(settings_factory(clients_file=None), lambda r: httpx.Response(200)) as c:
        assert (await c.post("/v1/identity/resolve", headers=auth(tokens["full"]),
                             json={"session_token": "s"})).status_code == 401
        assert (await c.get("/health/ready")).status_code == 503


# --- Identity -----------------------------------------------------------------------


async def test_identity_resolve_forwards_only_body_and_service_token(settings_factory, tokens):
    seen = {}

    def handler(request: httpx.Request):
        seen["auth"] = request.headers["authorization"]
        seen["rid"] = request.headers["x-request-id"]
        seen["body"] = request.content
        seen["forged"] = request.headers.get("x-user")
        return httpx.Response(200, json={"active": True, "subject": SUBJECT, "username": "customer001",
                                         "role": "customer", "csrf_valid": True,
                                         "session_expires_at": "2026-11-10T10:00:00+00:00"})

    async with adapter_client(settings_factory(), handler) as c:
        resp = await c.post("/v1/identity/resolve",
                            headers={**auth(tokens["identity"]), "X-Request-ID": "corr-12345678",
                                     "X-User": "opsadmin"},
                            json={"session_token": "sess", "csrf_token": "csrf"})
    assert resp.status_code == 200
    assert resp.json()["subject"] == SUBJECT and resp.json()["csrf_valid"] is True
    assert seen["auth"] == f"Bearer {tokens['core']}"  # the adapter's own credential, never the caller's
    assert seen["rid"] == "corr-12345678"
    assert resp.headers["X-Request-ID"] == "corr-12345678"
    assert seen["forged"] is None


async def test_inactive_identity(settings_factory, tokens):
    async with adapter_client(settings_factory(), lambda r: httpx.Response(200, json={"active": False})) as c:
        resp = await c.post("/v1/identity/resolve", headers=auth(tokens["identity"]), json={"session_token": "x"})
    assert resp.json() == {"active": False}


async def test_extra_fields_in_request_rejected(settings_factory, tokens):
    async with adapter_client(settings_factory(), lambda r: httpx.Response(200, json={"active": False})) as c:
        resp = await c.post("/v1/identity/resolve", headers=auth(tokens["identity"]),
                            json={"session_token": "x", "role": "admin"})
    assert resp.status_code == 422


# --- Error mapping and retries ---------------------------------------------------------


async def test_transient_core_errors_are_retried_then_succeed(settings_factory, tokens):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json=manifest_body())

    async with adapter_client(settings_factory(core_retry_attempts=3), handler) as c:
        resp = await c.get("/v1/statement-datasets/7/manifest", headers=auth(tokens["statements"]), params=PERIOD)
    assert resp.status_code == 200 and calls["n"] == 3


async def test_retries_are_bounded(settings_factory, tokens):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503)

    async with adapter_client(settings_factory(core_retry_attempts=3), handler) as c:
        resp = await c.get("/v1/statement-datasets/7/manifest", headers=auth(tokens["statements"]), params=PERIOD)
    assert resp.status_code == 503
    assert resp.json()["error"] == {**resp.json()["error"], "code": "core_unavailable", "retryable": True}
    assert resp.headers["Retry-After"]
    assert calls["n"] == 3


@pytest.mark.parametrize("status,code,expected_status,expected_code,expected_calls", [
    (404, "account_not_found", 404, "not_found", 1),
    (409, "period_not_settled", 409, "period_not_settled", 1),
    (422, "invalid_cursor", 422, "invalid_cursor", 1),
    (401, "invalid_client", 502, "core_auth_misconfigured", 1),
    (403, "insufficient_scope", 502, "core_auth_misconfigured", 1),
    (500, "ledger_reconciliation_failed", 502, "core_integrity_error", 1),
    (500, "something", 502, "core_error", 1),
])
async def test_core_error_mapping_without_retry(settings_factory, tokens, status, code, expected_status,
                                                expected_code, expected_calls):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(status, json={"error": {"code": code, "message": "m"}})

    async with adapter_client(settings_factory(), handler) as c:
        resp = await c.get("/v1/statement-datasets/7/manifest", headers=auth(tokens["statements"]), params=PERIOD)
    assert resp.status_code == expected_status
    assert resp.json()["error"]["code"] == expected_code
    assert calls["n"] == expected_calls


async def test_core_timeout_maps_to_503(settings_factory, tokens):
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    async with adapter_client(settings_factory(core_retry_attempts=2), handler) as c:
        resp = await c.get("/v1/statement-datasets/7/manifest", headers=auth(tokens["statements"]), params=PERIOD)
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "core_timeout"


async def test_core_connection_error_maps_to_503(settings_factory, tokens):
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    async with adapter_client(settings_factory(core_retry_attempts=1), handler) as c:
        resp = await c.get("/v1/statement-datasets/7/manifest", headers=auth(tokens["statements"]), params=PERIOD)
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "core_unavailable"


# --- Contract validation ---------------------------------------------------------------


@pytest.mark.parametrize("bad", [{"opening_balance_minor": 10.5}, {"opening_balance_minor": "1000"},
                                 {"dataset_version": "statement-v1"}, {"entries_sha256": "short"}, {"boundary": "[start,end] local"}])
async def test_contract_violation_fails_closed(settings_factory, tokens, bad):
    async with adapter_client(settings_factory(), lambda r: httpx.Response(200, json=manifest_body(**bad))) as c:
        resp = await c.get("/v1/statement-datasets/7/manifest", headers=auth(tokens["statements"]), params=PERIOD)
    assert resp.status_code == 502 and resp.json()["error"]["code"] == "core_contract_violation"


async def test_period_is_normalised_to_utc_and_naive_rejected(settings_factory, tokens):
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=manifest_body())

    async with adapter_client(settings_factory(), handler) as c:
        ok = await c.get("/v1/statement-datasets/7/manifest", headers=auth(tokens["statements"]), params=PERIOD)
        naive = await c.get("/v1/statement-datasets/7/manifest", headers=auth(tokens["statements"]),
                            params={**PERIOD, "period_start": "2026-09-01T00:00:00"})
        bad_subject = await c.get("/v1/statement-datasets/7/manifest", headers=auth(tokens["statements"]),
                                  params={**PERIOD, "subject": "customer001"})
    assert ok.status_code == 200
    assert seen["params"]["period_start"] == "2026-08-31T21:00:00+00:00"
    assert naive.status_code == 422 and bad_subject.status_code == 422


async def test_entries_page_size_capped(settings_factory, tokens):
    seen = {}

    def handler(request):
        seen["limit"] = request.url.params["limit"]
        return httpx.Response(200, json={"items": [], "has_more": False, "next_cursor": None})

    async with adapter_client(settings_factory(entries_max_page_size=100), handler) as c:
        resp = await c.get("/v1/statement-datasets/7/entries", headers=auth(tokens["statements"]),
                           params={**PERIOD, "limit": 100000})
    assert resp.status_code == 200 and seen["limit"] == "100"


# --- Core protection -------------------------------------------------------------------


async def test_concurrency_bound_rejects_excess(settings_factory, tokens):
    gate = asyncio.Event()
    inflight = {"max": 0, "now": 0}

    async def handler(request):
        inflight["now"] += 1
        inflight["max"] = max(inflight["max"], inflight["now"])
        await gate.wait()
        inflight["now"] -= 1
        return httpx.Response(200, json=manifest_body())

    s = settings_factory(core_max_concurrency=2, core_queue_timeout_seconds=0.05, core_retry_attempts=1)
    async with adapter_client(s, handler) as c:
        tasks = [asyncio.create_task(c.get("/v1/statement-datasets/7/manifest", headers=auth(tokens["statements"]),
                                           params=PERIOD)) for _ in range(5)]
        await asyncio.sleep(0.3)
        gate.set()
        results = await asyncio.gather(*tasks)
    codes = sorted(r.status_code for r in results)
    assert inflight["max"] <= 2
    assert codes.count(200) == 2 and codes.count(503) == 3
    assert all(r.json()["error"]["code"] == "adapter_busy" for r in results if r.status_code == 503)


async def test_rate_limit(settings_factory, tokens):
    s = settings_factory(core_rate_per_second=1, core_rate_burst=2, core_queue_timeout_seconds=0,
                         core_retry_attempts=1)
    async with adapter_client(s, lambda r: httpx.Response(200, json=manifest_body())) as c:
        codes = [(await c.get("/v1/statement-datasets/7/manifest", headers=auth(tokens["statements"]),
                              params=PERIOD)).status_code for _ in range(4)]
    assert codes[:2] == [200, 200] and 503 in codes[2:]


async def test_core_token_rotation_without_restart(settings_factory, tokens, tmp_path):
    import os
    import time

    seen = []
    s = settings_factory()
    async with adapter_client(s, lambda r: seen.append(r.headers["authorization"]) or
                              httpx.Response(200, json={"active": False})) as c:
        await c.post("/v1/identity/resolve", headers=auth(tokens["identity"]), json={"session_token": "x"})
        new = token()
        open(s.core_token_file, "w", encoding="utf-8").write(new)
        os.utime(s.core_token_file, (time.time() + 30, time.time() + 30))
        await c.post("/v1/identity/resolve", headers=auth(tokens["identity"]), json={"session_token": "x"})
    assert seen == [f"Bearer {tokens['core']}", f"Bearer {new}"]


# --- Health ------------------------------------------------------------------------


async def test_health(settings_factory):
    async with adapter_client(settings_factory(), lambda r: httpx.Response(503)) as c:
        assert (await c.get("/health/live")).status_code == 200
        ready = await c.get("/health/ready")
        assert ready.status_code == 200  # core outage does not flip readiness (documented)
    async with adapter_client(settings_factory(core_base_url="http://core.local"), lambda r: None) as c:
        body = (await c.get("/health/ready")).json()
        assert body["status"] == "not_ready" and any("http://" in p for p in body["problems"])


def test_tls_context_verifies_hostname(settings_factory):
    from core_adapter.core_client import build_ssl_context
    import ssl

    ctx = build_ssl_context(settings_factory())
    assert ctx.check_hostname is True and ctx.verify_mode == ssl.CERT_REQUIRED
