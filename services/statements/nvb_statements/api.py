"""statements-api: request, list, status, cancel and download customer statements.

Authentication: the browser's core session cookie (``nvb_session``) arrives through the
HTTP-aware gateway on the same origin. Every request is verified against the core through the
Core Adapter (no trusted identity headers; any client-supplied X-User/X-Role headers are ignored).
State-changing requests additionally require X-CSRF-Token, verified by the core for that session.
Ownership is checked server-side on every call: the request's owner subject must equal the
caller's subject AND the account must still belong to the caller (current authorization).
"""

from __future__ import annotations

import logging
import os
import re
import socket
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from nvb_statements.adapter_client import AdapterCallError, AdapterClient, Identity
from nvb_statements.config import Settings, get_settings
from nvb_statements.db import as_utc, build_engine, build_sessionmaker
from nvb_statements.logging_setup import configure_logging, request_id_var
from nvb_statements.models import StatementRequest, utcnow
from nvb_statements.period import PeriodError, month_period, validate_requestable
from nvb_statements.service import ServiceError, create_request, record_event
from nvb_statements.storage import ObjectStore, StorageError, build_store, sha256_hex

log = logging.getLogger("statements.api")
PREFIX = "/x/statements/v1"
SESSION_COOKIE = "nvb_session"
IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
API_REQUESTS = Counter("statements_api_requests_total", "statements-api requests", ["route", "status_class"])
CREATED = Counter("statements_requests_created_total", "Statement requests created", ["outcome"])


class CreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: int = Field(ge=1)
    period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="Calendar month YYYY-MM")


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, "request_id": request_id_var.get()}},
                        status_code=status)


def request_out(r: StatementRequest) -> dict:
    out = {
        "id": r.id,
        "status": r.status,
        "account": {"account_id": r.account_id, "masked_number": r.account_masked},
        "period": {"label": r.period_label, "timezone": r.period_timezone,
                   "start_utc": as_utc(r.period_start_utc).isoformat(),
                   "end_utc": as_utc(r.period_end_utc).isoformat(), "boundary": "[start,end)"},
        "created_at": as_utc(r.created_at).isoformat(),
        "completed_at": as_utc(r.completed_at).isoformat() if r.completed_at else None,
        "status_url": f"{PREFIX}/requests/{r.id}",
        "document_url": f"{PREFIX}/requests/{r.id}/document" if r.status == "completed" else None,
        "error": {"code": r.error_code, "message": r.error_message} if r.status == "failed" else None,
    }
    return out


def create_app(settings: Settings | None = None, *, adapter: AdapterClient | None = None,
               store: ObjectStore | None = None, sessionmaker=None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, "statements-api", f"{socket.gethostname()}:{os.getpid()}")

    @asynccontextmanager
    async def lifespan(_app):
        yield
        app.state.adapter.close()

    app = FastAPI(title="NVB Statements API", version="0.1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    if sessionmaker is None:
        sessionmaker = build_sessionmaker(build_engine(settings))
    app.state.sessionmaker = sessionmaker
    app.state.adapter = adapter or AdapterClient(settings)
    app.state.store = store
    app.state.settings = settings

    def get_store() -> ObjectStore:
        if app.state.store is None:
            app.state.store = build_store(settings)
        return app.state.store

    @app.middleware("http")
    async def correlate(request: Request, call_next):
        incoming = request.headers.get("x-request-id", "")
        rid = incoming if REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex
        tok = request_id_var.set(rid)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(tok)
        route = getattr(request.scope.get("route"), "path", "unmatched")
        API_REQUESTS.labels(route, f"{response.status_code // 100}xx").inc()
        response.headers["X-Request-ID"] = rid
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(ServiceError)
    async def _service_error(_r, exc: ServiceError):
        return _err(exc.status_code, exc.code, exc.message)

    def get_db():
        db = app.state.sessionmaker()
        try:
            yield db
        finally:
            db.close()

    def _identity(request: Request, *, mutation: bool) -> Identity:
        session = request.cookies.get(SESSION_COOKIE)
        if not session:
            raise ServiceError(401, "authentication_required", "Authentication required.")
        csrf = request.headers.get("x-csrf-token") if mutation else None
        if mutation and not csrf:
            raise ServiceError(403, "csrf_invalid", "Missing or invalid CSRF token.")
        try:
            ident = app.state.adapter.resolve(session, csrf, request_id_var.get())
        except AdapterCallError as exc:
            log.warning("identity check failed", extra={"code": exc.code})
            raise ServiceError(503, "identity_unavailable", "Identity service unavailable; retry later.") from None
        if ident is None:
            raise ServiceError(401, "authentication_required", "Authentication required.")
        if mutation and ident.csrf_valid is not True:
            raise ServiceError(403, "csrf_invalid", "Missing or invalid CSRF token.")
        if ident.role != "customer":
            raise ServiceError(403, "customer_only", "Customer access only.")
        return ident

    def reader(request: Request) -> Identity:
        return _identity(request, mutation=False)

    def writer(request: Request) -> Identity:
        return _identity(request, mutation=True)

    def _owned_request(db: Session, ident: Identity, request_id: str) -> StatementRequest:
        r = db.get(StatementRequest, request_id) if len(request_id) <= 36 else None
        if r is None or r.owner_subject != ident.subject or ident.owned_account(r.account_id) is None:
            # Same response for "missing", "someone else's" and "account no longer yours".
            raise ServiceError(404, "not_found", "Statement request not found.")
        return r

    # --- Health -----------------------------------------------------------------------

    @app.get("/health/live", include_in_schema=False)
    def live():
        return {"status": "alive"}

    @app.get("/health/ready", include_in_schema=False)
    def ready():
        """Ready = the statements DB answers. The adapter/core and object storage are not
        required to list/read status, so their outages degrade specific calls (503) instead."""
        try:
            with app.state.sessionmaker() as db:
                db.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001
            return JSONResponse({"status": "not_ready", "problems": ["database"]}, status_code=503)
        return {"status": "ready", "revision": settings.build_revision}

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # --- API ----------------------------------------------------------------------------

    @app.post(f"{PREFIX}/requests", status_code=202)
    def create(body: CreateBody, request: Request, ident: Identity = Depends(writer),
               idempotency_key: str | None = Header(default=None), db: Session = Depends(get_db)):
        if not idempotency_key or not IDEMPOTENCY_RE.match(idempotency_key):
            raise ServiceError(422, "idempotency_key_required", "Idempotency-Key header (8-64 chars) is required.")
        account = ident.owned_account(body.account_id)
        if account is None:
            raise ServiceError(404, "account_not_found", "Account not found.")
        try:
            period = month_period(body.period, settings.statement_timezone)
            validate_requestable(period, now=utcnow(), settle_seconds=settings.settle_seconds,
                                 max_months_back=settings.max_months_back)
        except PeriodError as exc:
            raise ServiceError(422, exc.code, str(exc)) from None
        result = create_request(db, owner_subject=ident.subject, requested_by=ident.subject, account=account,
                                period=period, idempotency_key=idempotency_key, queue=settings.queue_name)
        CREATED.labels("replayed" if result.replayed else "created").inc()
        out = request_out(result.request)
        resp = JSONResponse(out, status_code=202, headers={"Location": out["status_url"]})
        if result.replayed:
            resp.headers["Idempotent-Replayed"] = "true"
        return resp

    @app.get(f"{PREFIX}/requests")
    def list_requests(ident: Identity = Depends(reader), limit: int = Query(default=20, ge=1, le=100),
                      db: Session = Depends(get_db)):
        owned = {a["account_id"] for a in ident.accounts}
        rows = db.scalars(select(StatementRequest).where(StatementRequest.owner_subject == ident.subject)
                          .order_by(StatementRequest.created_at.desc(), StatementRequest.id).limit(limit)).all()
        return {"items": [request_out(r) for r in rows if r.account_id in owned]}

    @app.get(f"{PREFIX}/requests/{{request_id}}")
    def get_request(request_id: str, ident: Identity = Depends(reader), db: Session = Depends(get_db)):
        return request_out(_owned_request(db, ident, request_id))

    @app.post(f"{PREFIX}/requests/{{request_id}}/cancel")
    def cancel(request_id: str, ident: Identity = Depends(writer), db: Session = Depends(get_db)):
        r = _owned_request(db, ident, request_id)
        changed = db.execute(update(StatementRequest).where(StatementRequest.id == r.id,
                                                            StatementRequest.status == "queued")
                             .values(status="cancelled", updated_at=utcnow(), completed_at=utcnow()))
        if changed.rowcount != 1:
            db.rollback()
            raise ServiceError(409, "not_cancellable", "Only queued requests can be cancelled.")
        record_event(db, r.id, "cancelled", ident.subject)
        db.commit()
        db.refresh(r)
        return request_out(r)

    @app.get(f"{PREFIX}/requests/{{request_id}}/document")
    def download(request_id: str, ident: Identity = Depends(reader), db: Session = Depends(get_db)):
        r = _owned_request(db, ident, request_id)
        if r.status != "completed" or not r.result_key:
            raise ServiceError(409, "not_ready", "The statement is not ready.")
        try:
            data = get_store().get(r.result_key)
        except StorageError:
            raise ServiceError(503, "storage_unavailable", "Document storage unavailable; retry later.") from None
        if len(data) > settings.download_max_bytes or sha256_hex(data) != r.result_sha256:
            log.error("stored document failed integrity check", extra={"statement_request": r.id})
            raise ServiceError(500, "document_integrity_error", "The document failed an integrity check.")
        record_event(db, r.id, "downloaded", ident.subject)
        db.commit()
        filename = f"nvb-statement-{r.period_label}-{r.account_masked[-4:]}.pdf"
        return Response(data, media_type="application/pdf", headers={
            "Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff"})

    return app


def app_factory() -> FastAPI:
    return create_app()
