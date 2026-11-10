"""Core Adapter HTTP API (contract core-adapter-v1).

The only component that talks to the core. Extension services call it with their own
scoped service credentials; it calls the core with its own credential. It never reads
core tables and never holds core database credentials.
"""

from __future__ import annotations

import logging
import os
import re
import socket
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import ValidationError

from core_adapter import contract as c
from core_adapter.config import Settings, get_settings
from core_adapter.core_client import AdapterError, CoreClient
from core_adapter.credentials import Client, ClientRegistry
from core_adapter.logging_setup import configure_logging, request_id_var
from core_adapter.metrics import API_REQUESTS

log = logging.getLogger("adapter.api")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
SUBJECT_RE = r"^nvb-user:\d{1,12}:\d{1,12}$"


def _error(status: int, code: str, message: str, retryable: bool = False, headers: dict | None = None):
    return JSONResponse({"error": {"code": code, "message": message, "retryable": retryable,
                                   "request_id": request_id_var.get()}}, status_code=status, headers=headers)


def _validated(model, body):
    try:
        return model.model_validate(body)
    except ValidationError:
        log.error("core response violated contract", extra={"model": model.__name__})
        raise AdapterError(502, "core_contract_violation", "Core response did not match the contract.") from None


def create_app(settings: Settings | None = None, *, core_transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    settings = settings or get_settings()
    instance = settings.instance_id or f"{socket.gethostname()}:{os.getpid()}"
    configure_logging(settings.log_level, "core-adapter", instance)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await app.state.core.aclose()

    app = FastAPI(title="NVB Core Adapter", version=c.CONTRACT_VERSION, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.registry = ClientRegistry(settings.clients_file)
    app.state.core = CoreClient(settings, transport=core_transport)

    @app.middleware("http")
    async def correlate(request: Request, call_next):
        incoming = request.headers.get("x-request-id", "")
        rid = incoming if REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex
        token = request_id_var.set(rid)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        route = getattr(request.scope.get("route"), "path", "unmatched")
        API_REQUESTS.labels(route, f"{response.status_code // 100}xx").inc()
        response.headers["X-Request-ID"] = rid
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(AdapterError)
    async def _adapter_error(_r: Request, exc: AdapterError):
        headers = {"Retry-After": str(int(exc.retry_after or 1))} if exc.retryable else None
        return _error(exc.status_code, exc.code, exc.message, exc.retryable, headers)

    def require(*scopes: str):
        def dep(request: Request) -> Client:
            header = request.headers.get("authorization", "")
            token = header[7:].strip() if header[:7].lower() == "bearer " else None
            client = request.app.state.registry.authenticate(token)
            if client is None:
                raise AdapterError(401, "invalid_client", "Client authentication failed.")
            if not client.scopes.intersection(scopes):
                raise AdapterError(403, "insufficient_scope", "Client is not permitted to call this endpoint.")
            return client
        return dep

    def core(request: Request) -> CoreClient:
        return request.app.state.core

    # --- Health -------------------------------------------------------------------

    @app.get("/health/live", include_in_schema=False)
    def live():
        return {"status": "alive"}

    @app.get("/health/ready", include_in_schema=False)
    def ready(request: Request):
        """Ready = configuration is complete and credentials are loadable.

        A core outage does NOT make the adapter unready: callers then receive explicit
        503 core_unavailable errors (retryable) instead of connection failures, and
        restarts would not help. See docs/HEALTH_AND_READINESS.md.
        """
        problems = settings.validate_runtime()
        if settings.clients_file and not request.app.state.registry.is_valid():
            problems.append("clients file missing or invalid")
        if settings.core_token_file and not request.app.state.core.token.get():
            problems.append("core token file missing or empty")
        if problems:
            return JSONResponse({"status": "not_ready", "problems": problems}, status_code=503)
        return {"status": "ready", "contract": c.CONTRACT_VERSION, "revision": settings.build_revision}

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # --- Identity -----------------------------------------------------------------

    @app.post("/v1/identity/resolve", response_model=c.Identity, response_model_exclude_none=True)
    async def resolve(body: c.IdentityResolveRequest, client: Client = Depends(require("identity:resolve")),
                      cc: CoreClient = Depends(core)):
        if body.include_accounts and "accounts:read" not in client.scopes:
            raise AdapterError(403, "insufficient_scope", "include_accounts requires accounts:read.")
        resp = await cc.call("POST", "/internal/v1/sessions/introspect", endpoint="introspect",
                             request_id=request_id_var.get(), retry=True, json=body.model_dump())
        return _validated(c.Identity, resp.body)

    @app.get("/v1/subjects/{subject}/accounts", response_model=c.SubjectAccounts)
    async def subject_accounts(subject: str, _c: Client = Depends(require("accounts:read")),
                               cc: CoreClient = Depends(core)):
        if not re.match(SUBJECT_RE, subject):
            raise AdapterError(422, "invalid_subject", "Subject is not valid.")
        resp = await cc.call("GET", f"/internal/v1/subjects/{subject}/accounts", endpoint="subject_accounts",
                             request_id=request_id_var.get(), retry=True)
        return _validated(c.SubjectAccounts, resp.body)

    # --- Statements ---------------------------------------------------------------

    def _period_params(subject: str, period_start: datetime, period_end: datetime) -> dict:
        if not re.match(SUBJECT_RE, subject):
            raise AdapterError(422, "invalid_subject", "Subject is not valid.")
        if period_start.tzinfo is None or period_end.tzinfo is None:
            raise AdapterError(422, "invalid_period", "Period boundaries must include a UTC offset.")
        return {"subject": subject, "period_start": period_start.astimezone(UTC).isoformat(),
                "period_end": period_end.astimezone(UTC).isoformat()}

    @app.get("/v1/statement-datasets/{account_id}/manifest", response_model=c.StatementManifest)
    async def manifest(account_id: int, subject: str = Query(max_length=64), period_start: datetime = Query(),
                       period_end: datetime = Query(),
                       _c: Client = Depends(require("statements:read", "summaries:read")),
                       cc: CoreClient = Depends(core)):
        params = _period_params(subject, period_start, period_end)
        resp = await cc.call("GET", f"/internal/v1/accounts/{account_id}/statement-manifest",
                             endpoint="statement_manifest", request_id=request_id_var.get(), retry=True,
                             params=params)
        return _validated(c.StatementManifest, resp.body)

    @app.get("/v1/statement-datasets/{account_id}/entries", response_model=c.StatementEntriesPage)
    async def entries(account_id: int, subject: str = Query(max_length=64), period_start: datetime = Query(),
                      period_end: datetime = Query(), cursor: str | None = Query(default=None, max_length=200),
                      limit: int = Query(default=500, ge=1),
                      _c: Client = Depends(require("statements:read")), cc: CoreClient = Depends(core)):
        params = _period_params(subject, period_start, period_end)
        params["limit"] = min(limit, settings.entries_max_page_size)
        if cursor:
            params["cursor"] = cursor
        resp = await cc.call("GET", f"/internal/v1/accounts/{account_id}/statement-entries",
                             endpoint="statement_entries", request_id=request_id_var.get(), retry=True,
                             params=params)
        return _validated(c.StatementEntriesPage, resp.body)

    # --- Batch discovery ----------------------------------------------------------

    @app.get("/v1/discovery/statement-accounts", response_model=c.DiscoveryPage)
    async def discovery(after_account_id: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=500),
                        include_load_test: bool = False, _c: Client = Depends(require("discovery:read")),
                        cc: CoreClient = Depends(core)):
        resp = await cc.call("GET", "/internal/v1/discovery/statement-accounts", endpoint="discovery",
                             request_id=request_id_var.get(), retry=True,
                             params={"after_account_id": after_account_id, "limit": limit,
                                     "include_load_test": str(include_load_test).lower()})
        return _validated(c.DiscoveryPage, resp.body)

    return app


def app_factory() -> FastAPI:
    """uvicorn --factory core_adapter.app:app_factory"""
    return create_app()
