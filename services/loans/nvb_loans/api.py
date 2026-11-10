"""loans-api: loan application drafts, submission, review, recommendation and decision.

Identity comes from the core session cookie verified through the Core Adapter on every call
(see nvb_platform.webauth). Staff capabilities are explicit extension role assignments in THIS
service's database, bound to the caller's stable subject:

* customer        - own applications only (core role ``customer``; ownership by subject).
* reviewer        - applications ASSIGNED to them; writes a recommendation.
* approver        - assigns reviewers and takes the final decision. Never the reviewer who
                    recommended, never the applicant.
* batch_operator  - summary refresh batches.
* extension_admin - role administration and reviewer assignment.

Core admins/demo users get nothing automatically. "approved" is a workflow state: this service
has no code path that posts money or calls any core write API.
"""

from __future__ import annotations

import logging
import os
import re
import socket
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from nvb_loans import service as svc
from nvb_loans.config import Settings, get_settings
from nvb_loans.db import as_utc, build_engine, build_sessionmaker
from nvb_loans.models import (
    KNOWN_ROLES,
    OPEN_FOR_REVIEW,
    LoanApplication,
    LoanBatch,
    LoanEvent,
    RoleAssignment,
    RoleEvent,
    SummaryTask,
    utcnow,
)
from nvb_loans.service import ServiceError
from nvb_platform import roles as roles_lib
from nvb_platform.adapter_client import AdapterClient, Identity
from nvb_platform.logging_setup import configure_logging, request_id_var
from nvb_platform.webauth import AuthError, resolve_identity

log = logging.getLogger("loans.api")
PREFIX = "/x/loans/v1"
IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
API_REQUESTS = Counter("loans_api_requests_total", "loans-api requests", ["route", "status_class"])
TRANSITIONS = Counter("loans_transitions_total", "Application transitions", ["to_status"])
SUMMARY_LABEL = "SYNTHETIC DEMO SCORE - not a credit decision"


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DraftBody(_Body):
    account_id: StrictInt = Field(ge=1)
    amount_minor: StrictInt = Field(gt=0, description="integer minor units; floats are rejected")
    term_months: StrictInt = Field(gt=0)
    monthly_income_minor: StrictInt = Field(ge=0)
    purpose: str = Field(default="", max_length=200)


class UpdateBody(DraftBody):
    version: StrictInt = Field(ge=1)


class VersionBody(_Body):
    version: StrictInt = Field(ge=1)


class AssignBody(VersionBody):
    reviewer_subject: str = Field(pattern=r"^nvb-user:\d{1,12}:\d{1,12}$")


class RecommendBody(VersionBody):
    recommendation: str = Field(pattern=r"^(approve|reject)$")
    reason: str = Field(min_length=10, max_length=500)


class DecisionBody(VersionBody):
    decision: str = Field(pattern=r"^(approved|rejected)$")
    reason: str = Field(min_length=10, max_length=500)


class RoleBody(_Body):
    subject: str = Field(pattern=r"^nvb-user:\d{1,12}:\d{1,12}$")
    role: str = Field(min_length=1, max_length=32)
    reason: str = Field(default="", max_length=200)
    label: str = Field(default="", max_length=80)


def _ts(v):
    return as_utc(v).isoformat() if v else None


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, "request_id": request_id_var.get()}},
                        status_code=status)


def app_out(a: LoanApplication, summary: SummaryTask | None = None, *, staff: bool = False) -> dict:
    out = {
        "id": a.id, "status": a.status, "version": a.version,
        "account": {"account_id": a.account_id, "masked_number": a.account_masked},
        "currency": a.currency, "amount_minor": a.amount_minor, "term_months": a.term_months,
        "monthly_income_minor": a.monthly_income_minor, "purpose": a.purpose,
        "created_at": _ts(a.created_at), "submitted_at": _ts(a.submitted_at), "decided_at": _ts(a.decided_at),
        "decision_reason": a.decision_reason if a.status in ("approved", "rejected") else None,
        "disbursement": "none - workflow decision only; no money is posted by this system",
    }
    if staff:
        out.update({"owner_subject": a.owner_subject, "owner_label": a.owner_label,
                    "reviewer_subject": a.reviewer_subject, "recommendation": a.recommendation,
                    "recommendation_reason": a.recommendation_reason, "recommended_at": _ts(a.recommended_at),
                    "decided_by": a.decided_by,
                    "summary": None if summary is None else {
                        "status": summary.status, "label": SUMMARY_LABEL, "result": summary.result,
                        "error_code": summary.error_code, "completed_at": _ts(summary.completed_at)}})
    return out


def create_app(settings: Settings | None = None, *, adapter: AdapterClient | None = None, sessionmaker=None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, "loans-api", f"{socket.gethostname()}:{os.getpid()}")

    @asynccontextmanager
    async def lifespan(_app):
        yield
        app.state.adapter.close()

    app = FastAPI(title="NVB Loans API", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    if sessionmaker is None:
        sessionmaker = build_sessionmaker(build_engine(settings, "nvb-loans-api"))
    app.state.sessionmaker = sessionmaker
    app.state.adapter = adapter or AdapterClient(settings)
    injected = adapter is not None

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

    for exc_type in (ServiceError, AuthError, roles_lib.RoleError):
        @app.exception_handler(exc_type)
        async def _handler(_r, exc):
            return _err(exc.status_code, exc.code, exc.message)

    def get_db():
        db = app.state.sessionmaker()
        try:
            yield db
        finally:
            db.close()

    # --- identity ---------------------------------------------------------------------------

    def session_identity(request: Request) -> Identity:
        return resolve_identity(app.state.adapter, request, request_id=request_id_var.get())

    def customer(ident: Identity = Depends(session_identity)) -> Identity:
        if ident.role != "customer":
            raise ServiceError(403, "customer_only", "Customer access only.")
        return ident

    class Staff:
        def __init__(self, ident: Identity, roles: frozenset[str]) -> None:
            self.ident, self.roles, self.subject = ident, roles, ident.subject

        def has(self, *names: str) -> bool:
            return bool(self.roles.intersection(names))

    def staff(*required: str):
        def dep(ident: Identity = Depends(session_identity), db: Session = Depends(get_db)) -> Staff:
            held = roles_lib.active_roles(db, RoleAssignment, ident.subject)
            db.rollback()
            if not held.intersection(required):
                raise ServiceError(403, "forbidden", "You are not permitted to perform this operation.")
            return Staff(ident, held)
        return dep

    any_reviewer = staff("reviewer", "approver", "extension_admin")
    assigner = staff("approver", "extension_admin")
    reviewer = staff("reviewer")
    approver = staff("approver")
    batch_operator = staff("batch_operator")
    extension_admin = staff("extension_admin")

    def _owned(db: Session, ident: Identity, app_id: str) -> LoanApplication:
        a = db.get(LoanApplication, app_id) if len(app_id) <= 36 else None
        if a is None or a.owner_subject != ident.subject:
            raise ServiceError(404, "not_found", "Application not found.")   # no existence leak
        return a

    def _visible(db: Session, who: Staff, app_id: str) -> LoanApplication:
        """Reviewers see only applications assigned to them; approvers/admins see reviewable ones."""
        a = db.get(LoanApplication, app_id) if len(app_id) <= 36 else None
        if a is None or a.status == "draft":
            raise ServiceError(404, "not_found", "Application not found.")
        if not who.has("approver", "extension_admin") and a.reviewer_subject != who.subject:
            raise ServiceError(404, "not_found", "Application not found.")
        return a

    def _latest_summary(db: Session, app_id: str) -> SummaryTask | None:
        return db.scalar(select(SummaryTask).where(SummaryTask.application_id == app_id)
                         .order_by(SummaryTask.created_at.desc(), SummaryTask.id).limit(1))

    def _validate_terms(body: DraftBody) -> None:
        if body.amount_minor > settings.max_amount_minor:
            raise ServiceError(422, "amount_too_large", "The requested amount is above the demo limit.")
        if body.term_months > settings.max_term_months:
            raise ServiceError(422, "term_too_long", "The requested term is above the demo limit.")

    # --- health -------------------------------------------------------------------------------

    @app.get("/health/live", include_in_schema=False)
    def live():
        return {"status": "alive"}

    @app.get("/health/ready", include_in_schema=False)
    def ready():
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

    @app.get(f"{PREFIX}/me")
    def me(ident: Identity = Depends(session_identity), db: Session = Depends(get_db)):
        return {"subject": ident.subject, "username": ident.username, "display_name": ident.display_name,
                "core_role": ident.role,
                "extension_roles": sorted(roles_lib.active_roles(db, RoleAssignment, ident.subject))}

    # --- customer -----------------------------------------------------------------------------

    @app.post(f"{PREFIX}/applications", status_code=201)
    def create_draft(body: DraftBody, ident: Identity = Depends(customer), db: Session = Depends(get_db)):
        account = ident.owned_account(body.account_id)
        if account is None:
            raise ServiceError(404, "account_not_found", "Account not found.")
        _validate_terms(body)
        now = utcnow()
        a = LoanApplication(id=str(uuid.uuid4()), owner_subject=ident.subject, owner_label=ident.username[:80],
                            account_id=account["account_id"], account_masked=account["masked_number"][:16],
                            currency=account["currency"], amount_minor=body.amount_minor,
                            term_months=body.term_months, monthly_income_minor=body.monthly_income_minor,
                            purpose=body.purpose, status="draft", version=1, created_at=now, updated_at=now)
        db.add(a)
        svc.event(db, a.id, "draft_created", ident.subject, None, "draft")
        db.commit()
        return app_out(a)

    @app.get(f"{PREFIX}/applications")
    def my_applications(ident: Identity = Depends(customer), db: Session = Depends(get_db)):
        rows = db.scalars(select(LoanApplication).where(LoanApplication.owner_subject == ident.subject)
                          .order_by(LoanApplication.created_at.desc()).limit(100)).all()
        return {"items": [app_out(a) for a in rows]}

    @app.get(f"{PREFIX}/applications/{{app_id}}")
    def my_application(app_id: str, ident: Identity = Depends(customer), db: Session = Depends(get_db)):
        return app_out(_owned(db, ident, app_id))

    @app.put(f"{PREFIX}/applications/{{app_id}}")
    def update_draft(app_id: str, body: UpdateBody, ident: Identity = Depends(customer),
                     db: Session = Depends(get_db)):
        a = _owned(db, ident, app_id)
        account = ident.owned_account(body.account_id)
        if account is None:
            raise ServiceError(404, "account_not_found", "Account not found.")
        _validate_terms(body)
        svc.transition(db, a, body.version, ("draft",), {
            "account_id": account["account_id"], "account_masked": account["masked_number"][:16],
            "currency": account["currency"], "amount_minor": body.amount_minor, "term_months": body.term_months,
            "monthly_income_minor": body.monthly_income_minor, "purpose": body.purpose})
        svc.event(db, a.id, "draft_updated", ident.subject, "draft", "draft")
        db.commit()
        db.refresh(a)
        return app_out(a)

    @app.post(f"{PREFIX}/applications/{{app_id}}/submit")
    def submit(app_id: str, body: VersionBody, ident: Identity = Depends(customer), db: Session = Depends(get_db)):
        a = _owned(db, ident, app_id)
        if ident.owned_account(a.account_id) is None:
            raise ServiceError(409, "account_not_available", "The selected account is no longer available.")
        svc.transition(db, a, body.version, ("draft",), {"status": "submitted", "submitted_at": utcnow()})
        svc.event(db, a.id, "submitted", ident.subject, "draft", "submitted")
        svc.enqueue_summary(db, a.id, settings.queue_name)      # background summary; never blocks the customer
        db.commit()
        TRANSITIONS.labels("submitted").inc()
        db.refresh(a)
        return app_out(a)

    # --- staff: review ----------------------------------------------------------------------------

    @app.get(f"{PREFIX}/review/applications")
    def review_queue(who: Staff = Depends(any_reviewer), status: str | None = Query(default=None),
                     db: Session = Depends(get_db)):
        stmt = select(LoanApplication).where(LoanApplication.status != "draft")
        if not who.has("approver", "extension_admin"):
            stmt = stmt.where(LoanApplication.reviewer_subject == who.subject)
        if status:
            stmt = stmt.where(LoanApplication.status == status)
        else:
            stmt = stmt.where(or_(LoanApplication.status.in_(OPEN_FOR_REVIEW), LoanApplication.decided_at.is_not(None)))
        rows = db.scalars(stmt.order_by(LoanApplication.submitted_at.desc()).limit(200)).all()
        return {"items": [app_out(a, _latest_summary(db, a.id), staff=True) for a in rows]}

    @app.get(f"{PREFIX}/review/applications/{{app_id}}")
    def review_one(app_id: str, who: Staff = Depends(any_reviewer), db: Session = Depends(get_db)):
        a = _visible(db, who, app_id)
        events = db.scalars(select(LoanEvent).where(LoanEvent.application_id == a.id).order_by(LoanEvent.id)).all()
        return {**app_out(a, _latest_summary(db, a.id), staff=True),
                "events": [{"type": e.event_type, "actor": e.actor, "from": e.from_status, "to": e.to_status,
                            "at": _ts(e.created_at)} for e in events]}

    @app.post(f"{PREFIX}/review/applications/{{app_id}}/assign")
    def assign(app_id: str, body: AssignBody, who: Staff = Depends(assigner), db: Session = Depends(get_db)):
        a = _visible(db, who, app_id)
        if body.reviewer_subject == a.owner_subject:
            raise ServiceError(409, "conflict_of_interest", "The applicant cannot review their own application.")
        if "reviewer" not in roles_lib.active_roles(db, RoleAssignment, body.reviewer_subject):
            raise ServiceError(422, "not_a_reviewer", "The selected user does not hold the reviewer role.")
        if a.recommendation is not None:
            raise ServiceError(409, "invalid_state", "A recommendation already exists; reassignment is not allowed.")
        svc.transition(db, a, body.version, OPEN_FOR_REVIEW, {
            "status": "under_review", "reviewer_subject": body.reviewer_subject, "assigned_by": who.subject})
        svc.event(db, a.id, "assigned", who.subject, a.status, "under_review", reviewer=body.reviewer_subject)
        db.commit()
        TRANSITIONS.labels("under_review").inc()
        db.refresh(a)
        return app_out(a, _latest_summary(db, a.id), staff=True)

    @app.post(f"{PREFIX}/review/applications/{{app_id}}/recommendation")
    def recommend(app_id: str, body: RecommendBody, who: Staff = Depends(reviewer), db: Session = Depends(get_db)):
        a = db.get(LoanApplication, app_id) if len(app_id) <= 36 else None
        if a is None or a.reviewer_subject != who.subject:          # assignment is mandatory
            raise ServiceError(404, "not_found", "Application not found.")
        if a.owner_subject == who.subject:
            raise ServiceError(409, "conflict_of_interest", "The applicant cannot review their own application.")
        svc.transition(db, a, body.version, ("under_review",), {
            "recommendation": body.recommendation, "recommendation_reason": body.reason,
            "recommended_at": utcnow()})
        svc.event(db, a.id, "recommended", who.subject, "under_review", "under_review",
                  recommendation=body.recommendation)
        db.commit()
        db.refresh(a)
        return app_out(a, _latest_summary(db, a.id), staff=True)

    @app.post(f"{PREFIX}/review/applications/{{app_id}}/decision")
    def decide(app_id: str, body: DecisionBody, who: Staff = Depends(approver), db: Session = Depends(get_db)):
        a = _visible(db, who, app_id)
        if who.subject in (a.reviewer_subject, a.owner_subject):
            raise ServiceError(409, "separation_of_duties",
                               "The final decision must be taken by someone other than the reviewer or the applicant.")
        if a.status == "under_review" and a.recommendation is None:
            raise ServiceError(409, "recommendation_required", "A reviewer recommendation is required first.")
        svc.transition(db, a, body.version, ("under_review",), {
            "status": body.decision, "decision_reason": body.reason, "decided_by": who.subject,
            "decided_at": utcnow()})
        # Workflow decision ONLY: no disbursement, no core write of any kind.
        svc.event(db, a.id, "decided", who.subject, "under_review", body.decision,
                  followed_recommendation=(a.recommendation == ("approve" if body.decision == "approved" else "reject")))
        db.commit()
        TRANSITIONS.labels(body.decision).inc()
        db.refresh(a)
        return app_out(a, _latest_summary(db, a.id), staff=True)

    @app.get(f"{PREFIX}/review/reviewers")
    def reviewers(_w: Staff = Depends(assigner), db: Session = Depends(get_db)):
        rows = db.scalars(select(RoleAssignment).where(RoleAssignment.role == "reviewer",
                                                       RoleAssignment.active.is_(True))).all()
        return {"items": [{"subject": r.subject, "label": r.label} for r in rows]}

    # --- staff: summary batches ---------------------------------------------------------------------

    def _batch_out(db: Session, b: LoanBatch) -> dict:
        b = svc.finalize_batch(db, b)
        failures = db.scalars(select(SummaryTask).where(SummaryTask.batch_id == b.id, SummaryTask.status == "failed")
                              .limit(100)).all()
        return {"id": b.id, "kind": "loan_summaries", "status": b.status, "trigger": b.trigger_kind,
                "created_by": b.created_by, "progress": svc.batch_progress(db, b), "retry_rounds": b.retry_rounds,
                "created_at": _ts(b.created_at), "finished_at": _ts(b.finished_at),
                "failures": [{"task_id": t.id, "application_id": t.application_id, "error_code": t.error_code,
                              "attempts": t.attempts} for t in failures]}

    @app.post(f"{PREFIX}/batches", status_code=202)
    def create_batch(who: Staff = Depends(batch_operator), idempotency_key: str | None = Header(default=None),
                     db: Session = Depends(get_db)):
        if not idempotency_key or not IDEMPOTENCY_RE.match(idempotency_key):
            raise ServiceError(422, "idempotency_key_required", "Idempotency-Key header (8-64 chars) is required.")
        b, created = svc.create_summary_batch(db, queue=settings.queue_name, created_by=who.subject,
                                              trigger="manual", idempotency_key=f"manual-{idempotency_key}"[:64],
                                              max_applications=settings.batch_max_applications)
        resp = JSONResponse(_batch_out(db, b), status_code=202)
        if not created:
            resp.headers["Idempotent-Replayed"] = "true"
        return resp

    @app.get(f"{PREFIX}/batches")
    def list_batches(_w: Staff = Depends(batch_operator), db: Session = Depends(get_db)):
        rows = db.scalars(select(LoanBatch).order_by(LoanBatch.created_at.desc()).limit(20)).all()
        return {"items": [_batch_out(db, b) for b in rows]}

    @app.get(f"{PREFIX}/batches/{{batch_id}}")
    def get_batch(batch_id: str, _w: Staff = Depends(batch_operator), db: Session = Depends(get_db)):
        b = db.get(LoanBatch, batch_id) if len(batch_id) <= 36 else None
        if b is None:
            raise ServiceError(404, "not_found", "Batch not found.")
        return _batch_out(db, b)

    @app.post(f"{PREFIX}/batches/{{batch_id}}/retry-failed")
    def retry_batch(batch_id: str, _w: Staff = Depends(batch_operator), db: Session = Depends(get_db)):
        n = svc.retry_failed(db, batch_id, settings.queue_name, settings.batch_max_retry_rounds)
        return {**_batch_out(db, db.get(LoanBatch, batch_id)), "requeued": n}

    # --- staff: role administration -------------------------------------------------------------------

    @app.get(f"{PREFIX}/admin/roles")
    def list_roles(_w: Staff = Depends(extension_admin), db: Session = Depends(get_db)):
        return {"known_roles": sorted(KNOWN_ROLES), "items": roles_lib.list_assignments(db, RoleAssignment)}

    def _set_role(db: Session, who: Staff, body: RoleBody, active: bool) -> dict:
        if body.subject == who.subject and body.role == "extension_admin" and not active:
            raise ServiceError(409, "self_revoke_forbidden", "An administrator cannot revoke their own admin role.")
        changed = roles_lib.set_role(db, RoleAssignment, RoleEvent, subject=body.subject, role=body.role,
                                     active=active, actor=who.subject, known_roles=KNOWN_ROLES, reason=body.reason,
                                     label=body.label)
        db.commit()
        return {"changed": changed, "items": roles_lib.list_assignments(db, RoleAssignment)}

    @app.post(f"{PREFIX}/admin/roles/grant")
    def grant_role(body: RoleBody, who: Staff = Depends(extension_admin), db: Session = Depends(get_db)):
        return _set_role(db, who, body, True)

    @app.post(f"{PREFIX}/admin/roles/revoke")
    def revoke_role(body: RoleBody, who: Staff = Depends(extension_admin), db: Session = Depends(get_db)):
        return _set_role(db, who, body, False)

    return app


def app_factory() -> FastAPI:
    return create_app()
