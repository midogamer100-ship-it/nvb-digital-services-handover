"""statements-api: request, list, status, cancel and download customer statements; staff batches.

Authentication: the browser's core session cookie (``nvb_session``) arrives through the
HTTP-aware gateway on the same origin. Every request is verified against the core through the
Core Adapter (no trusted identity headers; any client-supplied X-User/X-Role headers are ignored).
State-changing requests additionally require X-CSRF-Token, verified by the core for that session.
Ownership is checked server-side on every call: the request's owner subject must equal the
caller's subject AND the account must still belong to the caller (current authorization).

Staff endpoints (batches, role administration) require an explicit extension role assignment
stored in THIS service's database for the caller's stable subject. The core role
(customer/admin/demo) grants nothing.
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

from nvb_platform import roles as roles_lib
from nvb_platform.webauth import SESSION_COOKIE, AuthError, resolve_identity
from nvb_statements import batch as batch_ops
from nvb_statements.adapter_client import AdapterCallError, AdapterClient, Identity
from nvb_statements.config import Settings, get_settings
from nvb_statements.db import as_utc, build_engine, build_sessionmaker
from nvb_statements.logging_setup import configure_logging, request_id_var
from nvb_statements.models import (
    KNOWN_ROLES,
    RoleAssignment,
    RoleEvent,
    StatementBatch,
    StatementRequest,
    utcnow,
)
from nvb_statements.period import PeriodError, month_period, validate_requestable
from nvb_statements.service import ServiceError, create_request, record_event
from nvb_statements.storage import ObjectStore, StorageError, build_store, sha256_hex

log = logging.getLogger("statements.api")
PREFIX = "/x/statements/v1"
IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
API_REQUESTS = Counter("statements_api_requests_total", "statements-api requests", ["route", "status_class"])
CREATED = Counter("statements_requests_created_total", "Statement requests created", ["outcome"])
assert SESSION_COOKIE == "nvb_session"


class CreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: int = Field(ge=1)
    period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="Calendar month YYYY-MM")


class BatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    include_load_test: bool = False


class RoleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str = Field(pattern=r"^nvb-user:\d{1,12}:\d{1,12}$")
    role: str = Field(min_length=1, max_length=32)
    reason: str = Field(default="", max_length=200)
    label: str = Field(default="", max_length=80)


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
        "purpose": r.purpose,
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

    app = FastAPI(title="NVB Statements API", version="0.2.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    if sessionmaker is None:
        # Fails at start-up (ConfigError) when the database is not explicitly configured:
        # there is no implicit SQLite fallback.
        sessionmaker = build_sessionmaker(build_engine(settings, "nvb-statements-api"))
    app.state.sessionmaker = sessionmaker
    app.state.adapter = adapter or AdapterClient(settings)
    app.state.store = store
    app.state.settings = settings
    injected = adapter is not None  # tests inject doubles; then runtime file checks do not apply

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

    @app.exception_handler(AuthError)
    async def _auth_error(_r, exc: AuthError):
        return _err(exc.status_code, exc.code, exc.message)

    @app.exception_handler(roles_lib.RoleError)
    async def _role_error(_r, exc: roles_lib.RoleError):
        return _err(exc.status_code, exc.code, exc.message)

    def get_db():
        db = app.state.sessionmaker()
        try:
            yield db
        finally:
            db.close()

    # --- Identity dependencies ---------------------------------------------------------

    def session_identity(request: Request) -> Identity:
        """Any authenticated core session (CSRF enforced for unsafe methods)."""
        return resolve_identity(app.state.adapter, request, request_id=request_id_var.get())

    def customer(ident: Identity = Depends(session_identity)) -> Identity:
        if ident.role != "customer":
            raise ServiceError(403, "customer_only", "Customer access only.")
        return ident

    def staff(*required: str):
        def dep(ident: Identity = Depends(session_identity), db: Session = Depends(get_db)) -> Identity:
            held = roles_lib.active_roles(db, RoleAssignment, ident.subject)
            db.rollback()  # end the authorization read transaction before the endpoint runs
            if not held.intersection(required):
                # Same answer whether the role exists or not; nothing is inferred from the core role.
                raise ServiceError(403, "forbidden", "You are not permitted to perform this operation.")
            return ident
        return dep

    batch_operator = staff("batch_operator")
    extension_admin = staff("extension_admin")

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
        """Ready = configuration is complete and the statements DB answers. The adapter/core and
        object storage are not probed: their outages degrade specific calls (503) instead, and a
        restart would not help."""
        problems = [] if injected else settings.validate_runtime("api")
        try:
            with app.state.sessionmaker() as db:
                db.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001
            problems.append("database")
        if problems:
            return JSONResponse({"status": "not_ready", "problems": problems}, status_code=503)
        return {"status": "ready", "revision": settings.build_revision}

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # --- Identity / accounts ------------------------------------------------------------

    @app.get(f"{PREFIX}/me")
    def me(ident: Identity = Depends(session_identity), db: Session = Depends(get_db)):
        """Who the core says the caller is, plus the caller's extension roles in this service."""
        return {"subject": ident.subject, "username": ident.username, "display_name": ident.display_name,
                "core_role": ident.role,
                "extension_roles": sorted(roles_lib.active_roles(db, RoleAssignment, ident.subject))}

    @app.get(f"{PREFIX}/accounts")
    def accounts(ident: Identity = Depends(customer)):
        """The caller's own accounts as the core reports them (masked numbers only)."""
        return {"items": ident.accounts, "statement_timezone": settings.statement_timezone}

    # --- Customer statements ----------------------------------------------------------------

    @app.post(f"{PREFIX}/requests", status_code=202)
    def create(body: CreateBody, request: Request, ident: Identity = Depends(customer),
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
    def list_requests(ident: Identity = Depends(customer), limit: int = Query(default=20, ge=1, le=100),
                      db: Session = Depends(get_db)):
        owned = {a["account_id"] for a in ident.accounts}
        rows = db.scalars(select(StatementRequest).where(StatementRequest.owner_subject == ident.subject)
                          .order_by(StatementRequest.created_at.desc(), StatementRequest.id).limit(limit)).all()
        return {"items": [request_out(r) for r in rows if r.account_id in owned]}

    @app.get(f"{PREFIX}/requests/{{request_id}}")
    def get_request(request_id: str, ident: Identity = Depends(customer), db: Session = Depends(get_db)):
        return request_out(_owned_request(db, ident, request_id))

    @app.post(f"{PREFIX}/requests/{{request_id}}/cancel")
    def cancel(request_id: str, ident: Identity = Depends(customer), db: Session = Depends(get_db)):
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
    def download(request_id: str, ident: Identity = Depends(customer), db: Session = Depends(get_db)):
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

    # --- Staff: batches (role batch_operator) --------------------------------------------------

    def _batch(db: Session, batch_id: str) -> StatementBatch:
        b = db.get(StatementBatch, batch_id) if len(batch_id) <= 36 else None
        if b is None:
            raise ServiceError(404, "not_found", "Batch not found.")
        return b

    @app.post(f"{PREFIX}/batches", status_code=202)
    def create_batch(body: BatchBody, ident: Identity = Depends(batch_operator),
                     idempotency_key: str | None = Header(default=None), db: Session = Depends(get_db)):
        """Create a monthly-statement batch. Same operation as the scheduled CronJob command."""
        if not idempotency_key or not IDEMPOTENCY_RE.match(idempotency_key):
            raise ServiceError(422, "idempotency_key_required", "Idempotency-Key header (8-64 chars) is required.")
        b, created = batch_ops.create_batch(db, settings, period_label=body.period, created_by=ident.subject,
                                            trigger="manual", idempotency_key=f"manual-{idempotency_key}"[:64],
                                            include_load_test=body.include_load_test)
        batch_id = b.id
        db.rollback()  # end this session's transaction before the resumable steps run on their own sessions
        try:
            batch_ops.advance_batch(app.state.sessionmaker, app.state.adapter, settings, batch_id)
        except AdapterCallError as exc:
            # The batch row and cursor are stored; POST again with the same key (or /resume) continues.
            log.warning("batch creation interrupted; resumable", extra={"batch_id": batch_id, "code": exc.code})
        db.expire_all()  # the steps above ran on other sessions; re-read the batch row
        out = batch_ops.batch_out(db, _batch(db, batch_id))
        resp = JSONResponse(out, status_code=202, headers={"Location": f"{PREFIX}/batches/{batch_id}"})
        if not created:
            resp.headers["Idempotent-Replayed"] = "true"
        return resp

    @app.get(f"{PREFIX}/batches")
    def list_batches(_i: Identity = Depends(batch_operator), limit: int = Query(default=20, ge=1, le=100),
                     db: Session = Depends(get_db)):
        rows = db.scalars(select(StatementBatch).order_by(StatementBatch.created_at.desc()).limit(limit)).all()
        return {"items": [batch_ops.batch_out(db, batch_ops.finalize_if_done(db, b)) for b in rows]}

    @app.get(f"{PREFIX}/batches/{{batch_id}}")
    def get_batch(batch_id: str, _i: Identity = Depends(batch_operator), db: Session = Depends(get_db)):
        b = batch_ops.finalize_if_done(db, _batch(db, batch_id))
        return {**batch_ops.batch_out(db, b), "failures": batch_ops.failed_items(db, b.id)}

    @app.post(f"{PREFIX}/batches/{{batch_id}}/resume")
    def resume_batch(batch_id: str, _i: Identity = Depends(batch_operator), db: Session = Depends(get_db)):
        _batch(db, batch_id)
        db.rollback()
        try:
            batch_ops.advance_batch(app.state.sessionmaker, app.state.adapter, settings, batch_id)
        except AdapterCallError:
            raise ServiceError(503, "core_unavailable", "The core adapter is unavailable; retry later.") from None
        db.expire_all()
        return batch_ops.batch_out(db, _batch(db, batch_id))

    @app.post(f"{PREFIX}/batches/{{batch_id}}/retry-failed")
    def retry_batch(batch_id: str, ident: Identity = Depends(batch_operator), db: Session = Depends(get_db)):
        requeued = batch_ops.retry_failed(db, settings, batch_id, ident.subject)
        db.expire_all()
        return {**batch_ops.batch_out(db, _batch(db, batch_id)), "requeued": requeued}

    @app.post(f"{PREFIX}/batches/{{batch_id}}/cancel")
    def cancel_batch(batch_id: str, ident: Identity = Depends(batch_operator), db: Session = Depends(get_db)):
        cancelled = batch_ops.cancel_batch(db, batch_id, ident.subject)
        db.expire_all()
        return {**batch_ops.batch_out(db, _batch(db, batch_id)), "cancelled_members": cancelled}

    # --- Staff: role administration (role extension_admin) ----------------------------------------

    @app.get(f"{PREFIX}/admin/roles")
    def list_roles(_i: Identity = Depends(extension_admin), db: Session = Depends(get_db)):
        return {"known_roles": sorted(KNOWN_ROLES), "items": roles_lib.list_assignments(db, RoleAssignment)}

    def _set_role(db: Session, ident: Identity, body: RoleBody, active: bool) -> dict:
        if body.subject == ident.subject and body.role == "extension_admin" and not active:
            raise ServiceError(409, "self_revoke_forbidden", "An administrator cannot revoke their own admin role.")
        changed = roles_lib.set_role(db, RoleAssignment, RoleEvent, subject=body.subject, role=body.role,
                                     active=active, actor=ident.subject, known_roles=KNOWN_ROLES,
                                     reason=body.reason, label=body.label)
        db.commit()
        log.info("role assignment changed" if changed else "role assignment unchanged",
                 extra={"target": body.subject, "role": body.role, "active": active})
        return {"changed": changed, "items": roles_lib.list_assignments(db, RoleAssignment)}

    @app.post(f"{PREFIX}/admin/roles/grant")
    def grant_role(body: RoleBody, ident: Identity = Depends(extension_admin), db: Session = Depends(get_db)):
        return _set_role(db, ident, body, True)

    @app.post(f"{PREFIX}/admin/roles/revoke")
    def revoke_role(body: RoleBody, ident: Identity = Depends(extension_admin), db: Session = Depends(get_db)):
        return _set_role(db, ident, body, False)

    return app


def app_factory() -> FastAPI:
    return create_app()
