"""Synchronous client for the Core Adapter (core-adapter-v1). Service credential from a file.

``settings`` is any object with ``adapter_base_url``, ``adapter_token_file``,
``adapter_ca_file`` and ``adapter_timeout_seconds``. TLS verification (CA + hostname) is
always on for https URLs; it cannot be disabled.
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from nvb_platform.secrets import read_secret


class AdapterCallError(Exception):
    def __init__(self, status_code: int, code: str, message: str, retryable: bool) -> None:
        super().__init__(f"{code}: {message}")
        self.status_code, self.code, self.message, self.retryable = status_code, code, message, retryable


@dataclass(frozen=True)
class Identity:
    subject: str
    role: str
    username: str
    display_name: str | None
    csrf_valid: bool | None
    accounts: list[dict] = field(default_factory=list)
    full_name: str | None = None

    def owned_account(self, account_id: int) -> dict | None:
        return next((a for a in self.accounts if a["account_id"] == account_id), None)


class AdapterClient:
    def __init__(self, settings, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        kwargs = {"base_url": settings.adapter_base_url, "timeout": settings.adapter_timeout_seconds,
                  "follow_redirects": False}
        if transport is not None:
            kwargs["transport"] = transport
        elif settings.adapter_base_url.startswith("https://"):
            ctx = ssl.create_default_context(cafile=settings.adapter_ca_file) if settings.adapter_ca_file \
                else ssl.create_default_context()
            ctx.check_hostname = True
            ctx.verify_mode = ssl.CERT_REQUIRED
            kwargs["verify"] = ctx
        self._http = httpx.Client(**kwargs)

    def close(self) -> None:
        self._http.close()

    def _headers(self, request_id: str | None) -> dict:
        token = read_secret(self.settings.adapter_token_file)
        if not token:
            raise AdapterCallError(503, "adapter_credentials_missing", "Adapter credential not configured.", True)
        h = {"Authorization": f"Bearer {token}"}
        if request_id:
            h["X-Request-ID"] = request_id
        return h

    def _call(self, method: str, path: str, request_id: str | None, **kw) -> dict:
        try:
            resp = self._http.request(method, path, headers=self._headers(request_id), **kw)
        except httpx.TimeoutException:
            raise AdapterCallError(503, "adapter_timeout", "Core adapter timed out.", True) from None
        except httpx.TransportError:
            raise AdapterCallError(503, "adapter_unavailable", "Core adapter unreachable.", True) from None
        if resp.status_code >= 400:
            try:
                err = resp.json()["error"]
                code, msg, retryable = err["code"], err.get("message", ""), bool(err.get("retryable"))
            except (ValueError, KeyError, TypeError):
                code, msg, retryable = "adapter_error", "Unexpected adapter response.", resp.status_code >= 500
            raise AdapterCallError(resp.status_code, code, msg, retryable)
        return resp.json()

    # --- Identity -------------------------------------------------------------------

    def resolve(self, session_token: str, csrf_token: str | None, request_id: str | None,
                include_accounts: bool = True) -> Identity | None:
        body = {"session_token": session_token, "include_accounts": include_accounts}
        if csrf_token is not None:
            body["csrf_token"] = csrf_token
        data = self._call("POST", "/v1/identity/resolve", request_id, json=body)
        if not data.get("active"):
            return None
        return Identity(subject=data["subject"], role=data["role"], username=data["username"],
                        display_name=data.get("display_name"), csrf_valid=data.get("csrf_valid"),
                        accounts=data.get("accounts") or [], full_name=data.get("full_name"))

    def subject_accounts(self, subject: str, request_id: str | None = None) -> list[dict]:
        return self._call("GET", f"/v1/subjects/{subject}/accounts", request_id)["accounts"]

    # --- Statement dataset --------------------------------------------------------------

    def _params(self, subject: str, start: datetime, end: datetime) -> dict:
        return {"subject": subject, "period_start": start.isoformat(), "period_end": end.isoformat()}

    def manifest(self, account_id: int, subject: str, start: datetime, end: datetime,
                 request_id: str | None = None) -> dict:
        return self._call("GET", f"/v1/statement-datasets/{account_id}/manifest", request_id,
                          params=self._params(subject, start, end))

    def entries(self, account_id: int, subject: str, start: datetime, end: datetime, cursor: str | None,
                limit: int, request_id: str | None = None) -> dict:
        params = {**self._params(subject, start, end), "limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self._call("GET", f"/v1/statement-datasets/{account_id}/entries", request_id, params=params)

    # --- Batch discovery ----------------------------------------------------------------

    def discovery(self, after_account_id: int, limit: int, request_id: str | None = None,
                  include_load_test: bool = False) -> dict:
        return self._call("GET", "/v1/discovery/statement-accounts", request_id,
                          params={"after_account_id": after_account_id, "limit": limit,
                                  "include_load_test": str(include_load_test).lower()})
