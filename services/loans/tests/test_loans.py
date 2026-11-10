"""Loan application workflow, authorization, separation of duties, summaries and batches."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from nvb_loans.models import LoanApplication, LoanEvent, OutboxMessage, SummaryTask
from nvb_loans.relay import publish_pending, reconcile
from nvb_loans.worker import Crash, Worker, last_full_months, score
from tests.conftest import SUBJECTS, account, browser, grant

B = "/x/loans/v1"
DRAFT = {"account_id": 101, "amount_minor": 12_000_000, "term_months": 24, "monthly_income_minor": 1_500_000,
         "purpose": "Home improvement (synthetic)"}
REASON = "Stable inflows over the reviewed months."


def draft(app, fake, who="alice", **over):
    r = browser(app, fake, who).post(f"{B}/applications", json={**DRAFT, **over})
    assert r.status_code == 201, r.text
    return r.json()


def submitted(app, fake, who="alice"):
    d = draft(app, fake, who, **({"account_id": 201} if who == "bob" else {}))
    r = browser(app, fake, who).post(f"{B}/applications/{d['id']}/submit", json={"version": d["version"]})
    assert r.status_code == 200, r.text
    return r.json()


def under_review(app, fake, reviewer="rev1"):
    a = submitted(app, fake)
    r = browser(app, fake, "appr").post(f"{B}/review/applications/{a['id']}/assign",
                                        json={"reviewer_subject": SUBJECTS[reviewer], "version": a["version"]})
    assert r.status_code == 200, r.text
    return r.json()


def recommended(app, fake, rec="approve"):
    a = under_review(app, fake)
    r = browser(app, fake, "rev1").post(f"{B}/review/applications/{a['id']}/recommendation",
                                        json={"recommendation": rec, "reason": REASON, "version": a["version"]})
    assert r.status_code == 200, r.text
    return r.json()


def drain(sm, broker, worker):
    publish_pending(sm, broker, 1000)
    out = []
    while (d := broker.get(worker.s.queue_name, 0)) is not None:
        out.append(worker.handle(d))
    return out


# --- customer: ownership and drafts ----------------------------------------------------------


def test_draft_submit_status_happy_path(app, fake, sm):
    d = draft(app, fake)
    assert d["status"] == "draft" and d["version"] == 1 and d["account"]["masked_number"] == "•••• 0101"
    assert "no money is posted" in d["disbursement"]
    alice = browser(app, fake, "alice")
    upd = alice.put(f"{B}/applications/{d['id']}", json={**DRAFT, "amount_minor": 9_000_000, "version": 1}).json()
    assert upd["amount_minor"] == 9_000_000 and upd["version"] == 2
    sub = alice.post(f"{B}/applications/{d['id']}/submit", json={"version": 2}).json()
    assert sub["status"] == "submitted" and sub["version"] == 3 and sub["submitted_at"]
    assert alice.put(f"{B}/applications/{d['id']}", json={**DRAFT, "version": 3}).status_code == 409  # no edits now
    assert [a["id"] for a in alice.get(f"{B}/applications").json()["items"]] == [d["id"]]
    with sm() as db:
        assert db.scalar(select(func.count()).select_from(SummaryTask)) == 1      # summary enqueued on submit
        assert db.scalar(select(func.count()).select_from(OutboxMessage)) == 1


def test_customer_cannot_use_another_customers_account_or_application(app, fake):
    assert browser(app, fake, "alice").post(f"{B}/applications", json={**DRAFT, "account_id": 201}).status_code == 404
    d = draft(app, fake)
    bob = browser(app, fake, "bob")
    assert bob.get(f"{B}/applications/{d['id']}").status_code == 404
    assert bob.post(f"{B}/applications/{d['id']}/submit", json={"version": 1}).status_code == 404
    assert bob.put(f"{B}/applications/{d['id']}", json={**DRAFT, "account_id": 201, "version": 1}).status_code == 404
    assert bob.get(f"{B}/applications").json()["items"] == []


def test_money_must_be_integer_minor_units_and_within_limits(app, fake):
    alice = browser(app, fake, "alice")
    assert alice.post(f"{B}/applications", json={**DRAFT, "amount_minor": 1200.50}).status_code == 422
    assert alice.post(f"{B}/applications", json={**DRAFT, "amount_minor": "12000"}).status_code == 422
    assert alice.post(f"{B}/applications", json={**DRAFT, "amount_minor": 0}).status_code == 422
    assert alice.post(f"{B}/applications", json={**DRAFT, "term_months": 500}).json()["error"]["code"] == "term_too_long"
    assert alice.post(f"{B}/applications", json={**DRAFT, "amount_minor": 10**12}
                      ).json()["error"]["code"] == "amount_too_large"
    assert alice.post(f"{B}/applications", json={**DRAFT, "role": "approver"}).status_code == 422   # unknown field


def test_authentication_csrf_and_identity_outage(app, fake):
    from fastapi.testclient import TestClient

    assert TestClient(app).get(f"{B}/applications").status_code == 401
    alice = browser(app, fake, "alice")
    alice.headers["X-CSRF-Token"] = fake.bob_csrf                     # another session's token
    assert alice.post(f"{B}/applications", json=DRAFT).status_code == 403
    alice.headers.pop("X-CSRF-Token")
    assert alice.post(f"{B}/applications", json=DRAFT).status_code == 403
    forged = browser(app, fake, "bob")
    forged.headers.update({"X-User": "alice", "X-Role": "approver", "X-Subject": SUBJECTS["appr"]})
    assert forged.get(f"{B}/review/applications").status_code == 403   # identity headers are ignored
    fake.unavailable = True
    assert browser(app, fake, "alice").get(f"{B}/applications").status_code == 503


# --- staff roles ----------------------------------------------------------------------------


def test_core_admin_without_extension_role_gets_nothing(app, fake, sm):
    a = submitted(app, fake)
    core_admin = browser(app, fake, "admin")                           # core role admin, no assignment yet
    assert core_admin.get(f"{B}/me").json() == {
        "subject": SUBJECTS["admin"], "username": "admin", "display_name": "admin", "core_role": "admin",
        "extension_roles": []}
    for method, path, body in (("get", "/review/applications", None), ("get", f"/review/applications/{a['id']}", None),
                               ("post", f"/review/applications/{a['id']}/decision",
                                {"decision": "approved", "reason": REASON, "version": a["version"]}),
                               ("get", "/admin/roles", None), ("get", "/batches", None)):
        r = getattr(core_admin, method)(f"{B}{path}", **({"json": body} if body else {}))
        assert r.status_code == 403, path
    assert browser(app, fake, "alice").get(f"{B}/review/applications").status_code == 403


def test_reviewer_sees_only_assigned_applications(app, fake, roles):
    a = under_review(app, fake, "rev1")
    other = submitted(app, fake, "bob")
    rev1, rev2 = browser(app, fake, "rev1"), browser(app, fake, "rev2")
    assert [x["id"] for x in rev1.get(f"{B}/review/applications").json()["items"]] == [a["id"]]
    assert rev2.get(f"{B}/review/applications").json()["items"] == []
    assert rev2.get(f"{B}/review/applications/{a['id']}").status_code == 404
    assert rev1.get(f"{B}/review/applications/{other['id']}").status_code == 404
    r = rev2.post(f"{B}/review/applications/{a['id']}/recommendation",
                  json={"recommendation": "approve", "reason": REASON, "version": a["version"]})
    assert r.status_code == 404                                         # not assigned -> cannot recommend
    assert {x["id"] for x in browser(app, fake, "appr").get(f"{B}/review/applications").json()["items"]} \
        == {a["id"], other["id"]}


def test_assignment_rules(app, fake, roles, sm):
    a = submitted(app, fake)
    appr = browser(app, fake, "appr")
    body = {"reviewer_subject": SUBJECTS["ops"], "version": a["version"]}
    assert appr.post(f"{B}/review/applications/{a['id']}/assign", json=body).json()["error"]["code"] == "not_a_reviewer"
    grant(sm, "alice", "reviewer")                                      # the applicant may hold a staff role...
    body = {"reviewer_subject": SUBJECTS["alice"], "version": a["version"]}
    assert appr.post(f"{B}/review/applications/{a['id']}/assign", json=body
                     ).json()["error"]["code"] == "conflict_of_interest"   # ...but never on their own application
    assert browser(app, fake, "rev1").post(f"{B}/review/applications/{a['id']}/assign",
                                           json={"reviewer_subject": SUBJECTS["rev1"], "version": a["version"]}
                                           ).status_code == 403          # reviewers cannot self-assign
    ok = appr.post(f"{B}/review/applications/{a['id']}/assign",
                   json={"reviewer_subject": SUBJECTS["rev1"], "version": a["version"]}).json()
    assert ok["status"] == "under_review" and ok["reviewer_subject"] == SUBJECTS["rev1"]


def test_recommendation_then_separate_decision(app, fake, roles, sm):
    a = recommended(app, fake, "approve")
    assert a["recommendation"] == "approve" and a["status"] == "under_review"
    r = browser(app, fake, "appr").post(f"{B}/review/applications/{a['id']}/decision",
                                        json={"decision": "approved", "reason": REASON, "version": a["version"]})
    done = r.json()
    assert r.status_code == 200 and done["status"] == "approved" and done["decided_by"] == SUBJECTS["appr"]
    mine = browser(app, fake, "alice").get(f"{B}/applications/{a['id']}").json()
    assert mine["status"] == "approved" and mine["decision_reason"] == REASON
    assert "reviewer_subject" not in mine and "summary" not in mine    # staff-only fields are not exposed
    with sm() as db:
        events = db.scalars(select(LoanEvent).where(LoanEvent.application_id == a["id"]).order_by(LoanEvent.id)).all()
    assert [(e.event_type, e.actor) for e in events] == [
        ("draft_created", SUBJECTS["alice"]), ("submitted", SUBJECTS["alice"]), ("assigned", SUBJECTS["appr"]),
        ("recommended", SUBJECTS["rev1"]), ("decided", SUBJECTS["appr"])]
    assert all(e.created_at for e in events)


def test_separation_of_duties(app, fake, roles, sm):
    a = recommended(app, fake)
    grant(sm, "rev1", "approver")                                       # the reviewer also holds approver
    r = browser(app, fake, "rev1").post(f"{B}/review/applications/{a['id']}/decision",
                                        json={"decision": "approved", "reason": REASON, "version": a["version"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "separation_of_duties"
    grant(sm, "alice", "approver")                                      # the applicant holds approver
    alice = browser(app, fake, "alice")
    r = alice.post(f"{B}/review/applications/{a['id']}/decision",
                   json={"decision": "approved", "reason": REASON, "version": a["version"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "separation_of_duties"
    # a reviewer without the approver role cannot decide at all
    assert browser(app, fake, "rev2").post(f"{B}/review/applications/{a['id']}/decision",
                                           json={"decision": "rejected", "reason": REASON, "version": a["version"]}
                                           ).status_code == 403


def test_invalid_transitions_and_required_reason(app, fake, roles):
    appr = browser(app, fake, "appr")
    a = submitted(app, fake)
    body = {"decision": "approved", "reason": REASON, "version": a["version"]}
    assert appr.post(f"{B}/review/applications/{a['id']}/decision", json=body
                     ).json()["error"]["code"] == "invalid_state"       # submitted -> approved is not allowed
    a = under_review(app, fake)
    assert appr.post(f"{B}/review/applications/{a['id']}/decision",
                     json={**body, "version": a["version"]}).json()["error"]["code"] == "recommendation_required"
    rev = browser(app, fake, "rev1")
    assert rev.post(f"{B}/review/applications/{a['id']}/recommendation",
                    json={"recommendation": "approve", "reason": "ok", "version": a["version"]}).status_code == 422
    assert rev.post(f"{B}/review/applications/{a['id']}/recommendation",
                    json={"recommendation": "maybe", "reason": REASON, "version": a["version"]}).status_code == 422
    a = recommended(app, fake)
    assert appr.post(f"{B}/review/applications/{a['id']}/decision",
                     json={"decision": "rejected", "reason": "no", "version": a["version"]}).status_code == 422
    done = appr.post(f"{B}/review/applications/{a['id']}/decision",
                     json={"decision": "rejected", "reason": REASON, "version": a["version"]}).json()
    assert done["status"] == "rejected"
    again = appr.post(f"{B}/review/applications/{a['id']}/decision",
                      json={"decision": "approved", "reason": REASON, "version": done["version"]})
    assert again.status_code == 409 and again.json()["error"]["code"] == "invalid_state"   # terminal


def test_stale_version_is_rejected(app, fake, roles):
    a = recommended(app, fake)
    appr = browser(app, fake, "appr")
    stale = {"decision": "approved", "reason": REASON, "version": a["version"] - 1}
    r = appr.post(f"{B}/review/applications/{a['id']}/decision", json=stale)
    assert r.status_code == 409 and r.json()["error"]["code"] == "version_conflict"
    assert appr.get(f"{B}/review/applications/{a['id']}").json()["status"] == "under_review"


def test_approval_does_not_touch_the_core(app, fake, roles):
    """The only core-facing calls of the whole workflow are identity resolution (read-only)."""
    a = recommended(app, fake)
    fake.manifest_calls.clear()
    writes = []
    fake.__dict__["post"] = lambda *x, **k: writes.append(x)
    browser(app, fake, "appr").post(f"{B}/review/applications/{a['id']}/decision",
                                    json={"decision": "approved", "reason": REASON, "version": a["version"]})
    assert writes == [] and fake.manifest_calls == []
    import nvb_loans.api as api_module
    import nvb_platform.adapter_client as client_module

    src = open(client_module.__file__, encoding="utf-8").read() + open(api_module.__file__, encoding="utf-8").read()
    for forbidden in ("/deposit", "/withdraw", "/transfers", "/api/accounts", "disburse("):
        assert forbidden not in src


# --- summary worker ---------------------------------------------------------------------------


def test_summary_is_integer_only_and_labelled_synthetic(app, fake, sm, broker, worker, roles):
    a = under_review(app, fake)
    assert drain(sm, broker, worker) == ["completed"]
    s = browser(app, fake, "rev1").get(f"{B}/review/applications/{a['id']}").json()["summary"]
    res = s["result"]
    assert s["status"] == "completed" and "SYNTHETIC DEMO SCORE" in s["label"] and res["model"] == "synthetic-demo-v1"
    assert res["installment_minor"] == 500_000 and res["installment_to_credits_bps"] == 5555
    assert res["band"] == "higher_indicative_risk" and len(res["months"]) == 3
    assert all(isinstance(v, int) for k, v in res.items() if k.endswith(("_minor", "_bps")))
    assert len(fake.manifest_calls) == 3 and {c[0] for c in fake.manifest_calls} == {101}
    assert a["status"] == "under_review"                                # the score never changes the workflow state


def test_score_function_edges():
    months = [{"period": "2026-08", "credit_total_minor": 0, "debit_total_minor": 0, "closing_balance_minor": 0,
               "entry_count": 0}]
    assert score(amount_minor=1000, term_months=3, declared_income_minor=0, months=months)["band"] \
        == "insufficient_history"
    assert score(amount_minor=1000, term_months=3, declared_income_minor=0, months=months)["installment_minor"] == 334
    good = [{**months[0], "credit_total_minor": 1_000_000, "debit_total_minor": 400_000}]
    assert score(amount_minor=1_200_000, term_months=12, declared_income_minor=0, months=good)["band"] \
        == "lower_indicative_risk"


def test_last_full_months_use_cairo_boundaries():
    from datetime import UTC, datetime

    got = last_full_months(datetime(2026, 11, 10, 12, tzinfo=UTC), "Africa/Cairo", 2, 600)
    assert [g[0] for g in got] == ["2026-09", "2026-10"]
    assert got[1][1] == datetime(2026, 9, 30, 21, tzinfo=UTC)           # 1 Oct 00:00 EEST (UTC+3)
    assert got[1][2] == datetime(2026, 10, 31, 22, tzinfo=UTC)          # 1 Nov 00:00 EET (UTC+2), DST ended
    # just after local midnight on the 1st the previous month is not yet settled
    early = last_full_months(datetime(2026, 10, 31, 22, 5, tzinfo=UTC), "Africa/Cairo", 1, 600)
    assert early[0][0] == "2026-09"


def test_summary_retries_then_fails_bounded_when_core_is_down(app, fake, sm, broker, worker):
    submitted(app, fake)
    fake.unavailable = True
    outcomes = []
    for _ in range(3):
        outcomes += drain(sm, broker, worker)
        reconcile(sm, worker.s.queue_name)
    assert outcomes == ["retry_scheduled", "retry_scheduled", "failed"]
    with sm() as db:
        t = db.scalar(select(SummaryTask))
        assert t.status == "failed" and t.error_code == "attempts_exhausted" and t.result is None


def test_summary_fails_closed_when_account_is_no_longer_owned(app, fake, sm, broker, worker):
    submitted(app, fake)
    fake.owner[101] = "nvb-user:999:1"
    assert drain(sm, broker, worker) == ["failed"]
    with sm() as db:
        assert db.scalar(select(SummaryTask)).error_code == "authorization_revoked"


@pytest.mark.parametrize("point", ["after_claim", "before_commit", "after_commit", "before_ack"])
def test_summary_worker_crash_points_recover(app, fake, sm, broker, settings, point):
    from datetime import timedelta

    from sqlalchemy import update

    from nvb_loans.models import utcnow

    submitted(app, fake)
    publish_pending(sm, broker, 10)
    crashing = Worker(settings, sm, broker, fake, worker_id="crasher", crash_at=point)
    with pytest.raises(Crash):
        crashing.handle(broker.get(settings.queue_name, 0))
    broker.crash_consumer()                                             # unacked message is redelivered
    with sm() as db:
        db.execute(update(SummaryTask).where(SummaryTask.status == "running")
                   .values(lease_expires_at=utcnow() - timedelta(seconds=1)))
        db.commit()
    survivor = Worker(settings, sm, broker, fake, worker_id="survivor")
    out = survivor.handle(broker.get(settings.queue_name, 0))
    assert out == ("already_completed" if point in ("after_commit", "before_ack") else "completed")
    with sm() as db:
        t = db.scalar(select(SummaryTask))
        assert t.status == "completed" and t.result["model"] == "synthetic-demo-v1"
    assert not broker.unacked and broker.depth(settings.queue_name) == 0


def test_malformed_message_is_dead_lettered_and_unexpected_error_is_not_acked(sm, broker, worker, settings):
    broker.publish(settings.queue_name, "m1", {"type": "something.else"})
    assert worker.handle(broker.get(settings.queue_name, 0)) == "dead_lettered" and len(broker.dead) == 1
    broker.publish(settings.queue_name, "m2", {"type": "loan.summarize", "task_id": str(uuid.uuid4())})
    worker.sm = lambda: (_ for _ in ()).throw(RuntimeError("database down"))
    assert worker.run(drain=True, max_messages=5) == 2
    assert broker.depth(settings.queue_name) == 1 and not broker.unacked


# --- batches -----------------------------------------------------------------------------------


def test_summary_batch_freezes_members_reports_progress_and_bounded_retry(app, fake, sm, broker, worker, roles,
                                                                           settings):
    a1, a2 = submitted(app, fake), submitted(app, fake, "bob")
    draft(app, fake)                                                    # drafts are not part of the manifest
    drain(sm, broker, worker)                                           # the on-submit summaries
    ops = browser(app, fake, "ops")
    assert browser(app, fake, "appr").post(f"{B}/batches", headers={"Idempotency-Key": "k-12345678"}).status_code == 403
    key = {"Idempotency-Key": uuid.uuid4().hex}
    fake.owner[201] = "nvb-user:999:1"                                  # bob's summary will fail
    r = ops.post(f"{B}/batches", headers=key)
    b = r.json()
    assert r.status_code == 202 and b["progress"] == {"queued": 2, "running": 0, "completed": 0, "failed": 0, "total": 2}
    assert ops.post(f"{B}/batches", headers=key).json()["id"] == b["id"]            # idempotent
    submitted(app, fake)                                                # arrives after the freeze: not a member
    out = drain(sm, broker, worker)
    assert sorted(out) == ["completed", "completed", "failed"]          # 2 members + the new on-submit summary
    got = ops.get(f"{B}/batches/{b['id']}").json()
    assert got["status"] == "completed_with_failures" and got["progress"]["failed"] == 1
    assert got["failures"][0]["application_id"] == a2["id"] and got["failures"][0]["error_code"] == "authorization_revoked"
    fake.owner[201] = SUBJECTS["bob"]
    retried = ops.post(f"{B}/batches/{b['id']}/retry-failed").json()
    assert retried["requeued"] == 1 and retried["status"] == "running"
    assert drain(sm, broker, worker) == ["completed"]
    assert ops.get(f"{B}/batches/{b['id']}").json()["status"] == "completed"
    assert ops.post(f"{B}/batches/{b['id']}/retry-failed").json()["error"]["code"] == "nothing_to_retry"
    assert a1["id"] != a2["id"] and account(1)


def test_scheduled_batch_command_uses_the_same_operation(app, fake, sm, settings):
    from nvb_loans import service as svc

    submitted(app, fake)
    with sm() as db:
        b1, c1 = svc.create_summary_batch(db, queue=settings.queue_name, created_by="system:schedule",
                                          trigger="schedule", idempotency_key="schedule-2026-11-10",
                                          max_applications=10)
        b2, c2 = svc.create_summary_batch(db, queue=settings.queue_name, created_by="system:schedule",
                                          trigger="schedule", idempotency_key="schedule-2026-11-10",
                                          max_applications=10)
    assert c1 and not c2 and b1.id == b2.id and b1.total_items == 1
    with sm() as db, pytest.raises(svc.ServiceError):
        svc.create_summary_batch(db, queue=settings.queue_name, created_by="x", trigger="schedule",
                                 idempotency_key="other-key-1", max_applications=0)


def test_role_administration(app, fake, roles, sm):
    admin = browser(app, fake, "admin")
    r = admin.post(f"{B}/admin/roles/grant", json={"subject": SUBJECTS["bob"], "role": "reviewer", "label": "bob"})
    assert r.json()["changed"] is True
    assert admin.post(f"{B}/admin/roles/grant", json={"subject": SUBJECTS["bob"], "role": "superuser"}
                      ).json()["error"]["code"] == "unknown_role"
    assert admin.post(f"{B}/admin/roles/revoke", json={"subject": SUBJECTS["admin"], "role": "extension_admin"}
                      ).json()["error"]["code"] == "self_revoke_forbidden"
    assert browser(app, fake, "appr").post(f"{B}/admin/roles/grant",
                                           json={"subject": SUBJECTS["appr"], "role": "extension_admin"}
                                           ).status_code == 403          # approvers cannot grant themselves roles
    assert {i["label"] for i in browser(app, fake, "appr").get(f"{B}/review/reviewers").json()["items"]} \
        == {"rev1", "rev2", "bob"}


def test_no_implicit_sqlite(monkeypatch):
    from nvb_loans.config import Settings
    from nvb_platform.db import ConfigError

    with pytest.raises(ConfigError, match="LOANS_DATABASE_URL is not set"):
        Settings().resolved_database_url()
    text = " ".join(Settings().validate_runtime("worker"))
    assert "LOANS_ADAPTER_TOKEN_FILE" in text and "LOANS_BROKER_URL" in text
    assert LoanApplication.__tablename__ == "loan_applications"
