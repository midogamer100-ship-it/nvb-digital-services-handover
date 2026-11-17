"""Operations Admin loan desk: the separate reviewer workflow is retired (default configuration)."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from nvb_loans import admin as admin_cli
from nvb_loans.models import LoanApplication, LoanEvent, RoleAssignment, RoleEvent
from tests.conftest import SUBJECTS, browser, grant
from tests.test_loans import B, REASON, submitted


@pytest.fixture
def desk(sm):
    grant(sm, "desk", "operations_admin")       # core role admin + explicit assignment
    grant(sm, "desk2", "operations_admin")


def act(client, app_id, action, version, **body):
    return client.post(f"{B}/review/applications/{app_id}/{action}", json={"version": version, **body})


def legacy_held(sm, app_id, reviewer, recommendation=None):
    """Simulate data left by the retired workflow: assigned to a reviewer (optionally recommended)."""
    with sm() as db:
        a = db.get(LoanApplication, app_id)
        a.status, a.reviewer_subject, a.assigned_by, a.version = "under_review", SUBJECTS[reviewer], SUBJECTS["appr"], \
            a.version + 1
        if recommendation:
            a.recommendation, a.recommendation_reason = recommendation, REASON
        db.add(LoanEvent(application_id=app_id, event_type="assigned", actor=SUBJECTS["appr"],
                         from_status="submitted", to_status="under_review", details={"reviewer": SUBJECTS[reviewer]}))
        if recommendation:
            db.add(LoanEvent(application_id=app_id, event_type="recommended", actor=SUBJECTS[reviewer],
                             from_status="under_review", to_status="under_review",
                             details={"recommendation": recommendation}))
        db.commit()
        return a.version


def test_operations_admin_reviews_and_decides_end_to_end(app, fake, sm, desk):
    a = submitted(app, fake)
    d = browser(app, fake, "desk")
    me = d.get(f"{B}/me").json()
    assert me["effective_roles"] == ["operations_admin"] and me["capabilities"]["loan_management"] is True
    assert me["capabilities"]["legacy_reviewer_workflow"] is False
    stats = d.get(f"{B}/review/stats").json()
    assert stats["by_status"]["submitted"] == 1 and stats["pending_review"] == 1 and stats["pending_decision"] == 0
    assert [x["id"] for x in d.get(f"{B}/review/applications?queue=pending_review").json()["items"]] == [a["id"]]

    started = act(d, a["id"], "start-review", a["version"]).json()
    assert started["status"] == "under_review" and started["reviewer_subject"] == SUBJECTS["desk"]
    r = act(d, a["id"], "decision", started["version"], decision="approved", reason=REASON)
    assert r.status_code == 409 and r.json()["error"]["code"] == "recommendation_required"
    rec = act(d, a["id"], "recommendation", started["version"], recommendation="approve", reason=REASON).json()
    assert d.get(f"{B}/review/stats").json()["pending_decision"] == 1
    done = act(d, a["id"], "decision", rec["version"], decision="approved", reason=REASON)
    assert done.status_code == 200 and done.json()["status"] == "approved"
    assert "no money is posted" in done.json()["disbursement"]
    again = act(d, a["id"], "decision", done.json()["version"], decision="rejected", reason=REASON)
    assert again.status_code == 409 and again.json()["error"]["code"] == "invalid_state"   # duplicate decision
    detail = d.get(f"{B}/review/applications/{a['id']}").json()
    assert [e["type"] for e in detail["events"]] == ["draft_created", "submitted", "review_started", "recommended",
                                                     "decided"]
    assert detail["assignment"]["reviewer_is_current_admin"] is True


def test_access_requires_core_admin_and_explicit_assignment(app, fake, sm, desk):
    a = submitted(app, fake)
    grant(sm, "bob", "operations_admin")                              # a customer holding the row gets nothing
    paths = [("get", "/review/applications", None), ("get", "/review/stats", None),
             ("get", f"/review/applications/{a['id']}", None),
             ("post", f"/review/applications/{a['id']}/start-review", {"version": a["version"]})]
    for who in ("bob", "alice", "admin"):                             # admin: core admin WITHOUT the assignment
        c = browser(app, fake, who)
        for method, path, body in paths:
            r = getattr(c, method)(f"{B}{path}", **({"json": body} if body else {}))
            assert r.status_code == 403, (who, path)
    assert browser(app, fake, "bob").get(f"{B}/me").json()["capabilities"]["loan_management"] is False


def test_former_reviewer_gets_nothing_while_retired_and_everything_back_on_rollback(app, fake, sm, roles):
    a = submitted(app, fake)
    version = legacy_held(sm, a["id"], "rev1")
    rev = browser(app, fake, "rev1")
    assert rev.get(f"{B}/review/applications").status_code == 403
    assert act(rev, a["id"], "recommendation", version, recommendation="approve", reason=REASON).status_code == 403
    assert act(browser(app, fake, "appr"), a["id"], "decision", version, decision="approved",
               reason=REASON).status_code == 403
    assert rev.get(f"{B}/me").json()["extension_roles"] == ["reviewer"]           # row kept (history)
    assert rev.get(f"{B}/me").json()["effective_roles"] == []
    appr = browser(app, fake, "appr")                                 # retired approver: no access at all
    assert appr.get(f"{B}/review/reviewers").status_code == 403
    ext_admin = browser(app, fake, "admin")                           # extension_admin keeps role admin only
    assert ext_admin.get(f"{B}/review/reviewers").status_code == 410
    r = act(ext_admin, a["id"], "assign", version, reviewer_subject=SUBJECTS["rev2"])
    assert r.status_code == 410 and r.json()["error"]["code"] == "reviewer_workflow_retired"


@pytest.mark.legacy_reviewers
def test_rollback_flag_restores_the_reviewer_workflow(app, fake, sm, roles):
    a = submitted(app, fake)
    version = legacy_held(sm, a["id"], "rev1")
    rev = browser(app, fake, "rev1")
    assert [x["id"] for x in rev.get(f"{B}/review/applications").json()["items"]] == [a["id"]]
    assert act(rev, a["id"], "recommendation", version, recommendation="approve", reason=REASON).status_code == 200


def test_takeover_of_application_held_by_retired_reviewer_keeps_history(app, fake, sm, roles, desk):
    a = submitted(app, fake)
    version = legacy_held(sm, a["id"], "rev1")
    d = browser(app, fake, "desk")
    assert d.get(f"{B}/review/stats").json()["held_by_retired_reviewer"] == 1
    detail = d.get(f"{B}/review/applications/{a['id']}").json()
    assert detail["assignment"]["held_by_retired_reviewer"] is True
    taken = act(d, a["id"], "start-review", version)
    assert taken.status_code == 200 and taken.json()["reviewer_subject"] == SUBJECTS["desk"]
    events = d.get(f"{B}/review/applications/{a['id']}").json()["events"]
    assert events[-2]["type"] == "assigned" and events[-2]["details"]["reviewer"] == SUBJECTS["rev1"]
    assert events[-1]["type"] == "review_started" and events[-1]["details"]["previous_reviewer"] == SUBJECTS["rev1"]
    assert d.get(f"{B}/review/stats").json()["held_by_retired_reviewer"] == 0


def test_legacy_recommendation_is_decided_directly(app, fake, sm, roles, desk):
    a = submitted(app, fake)
    version = legacy_held(sm, a["id"], "rev1", recommendation="reject")
    d = browser(app, fake, "desk")
    r = act(d, a["id"], "start-review", version)
    assert r.status_code == 409 and r.json()["error"]["code"] == "invalid_state"     # recommendation preserved
    done = act(d, a["id"], "decision", version, decision="rejected", reason=REASON).json()
    assert done["status"] == "rejected" and done["recommendation"] == "reject"
    assert done["reviewer_subject"] == SUBJECTS["rev1"]                                # historical identity kept


def test_admin_cannot_review_or_decide_own_application(app, fake, sm, desk):
    fake.sessions[fake.desk_token]["role"] = "customer"
    fake.sessions[fake.desk_token]["accounts"] = [{"account_id": 301, "masked_number": "•••• 0301", "currency": "EGP"}]
    fake.owner[301] = SUBJECTS["desk"]
    d = browser(app, fake, "desk")
    r = d.post(f"{B}/applications", json={"account_id": 301, "amount_minor": 1_000_000, "term_months": 12,
                                         "monthly_income_minor": 500_000, "purpose": "own"})
    own = d.post(f"{B}/applications/{r.json()['id']}/submit", json={"version": 1}).json()
    fake.sessions[fake.desk_token]["role"] = "admin"                   # same person, now acting as admin
    r = act(d, own["id"], "start-review", own["version"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "conflict_of_interest"
    other = browser(app, fake, "desk2")
    started = act(other, own["id"], "start-review", own["version"]).json()
    rec = act(other, own["id"], "recommendation", started["version"], recommendation="approve", reason=REASON).json()
    r = act(d, own["id"], "decision", rec["version"], decision="approved", reason=REASON)
    assert r.status_code == 409 and r.json()["error"]["code"] == "separation_of_duties"


def test_stale_and_concurrent_actions_are_rejected(app, fake, sm, desk):
    a = submitted(app, fake)
    d, d2 = browser(app, fake, "desk"), browser(app, fake, "desk2")
    assert act(d, a["id"], "start-review", a["version"]).status_code == 200
    r = act(d2, a["id"], "start-review", a["version"])                # second admin, stale version
    assert r.status_code == 409 and r.json()["error"]["code"] == "version_conflict"
    r = act(d2, a["id"], "recommendation", a["version"] + 1, recommendation="approve", reason=REASON)
    assert r.status_code == 404                                       # the review belongs to the first admin
    r = act(d, a["id"], "recommendation", a["version"] + 1, recommendation="approve", reason="short")
    assert r.status_code == 422                                       # meaningful reason required


def test_queue_search_and_customer_flow_unchanged(app, fake, sm, desk):
    a, b = submitted(app, fake), submitted(app, fake, "bob")
    d = browser(app, fake, "desk")
    assert [x["id"] for x in d.get(f"{B}/review/applications", params={"q": "BOB"}).json()["items"]] == [b["id"]]
    assert [x["id"] for x in d.get(f"{B}/review/applications", params={"q": a["id"]}).json()["items"]] == [a["id"]]
    assert d.get(f"{B}/review/applications", params={"q": "%"}).json()["items"] == []
    assert d.get(f"{B}/review/applications", params={"queue": "bogus"}).status_code == 422
    assert browser(app, fake, "alice").get(f"{B}/applications").json()["items"][0]["status"] == "submitted"
    assert browser(app, fake, "alice").get(f"{B}/review/applications").status_code == 403


def test_reviewer_transition_dry_run_and_apply(app, fake, sm, roles, desk, capsys):
    a = submitted(app, fake)
    legacy_held(sm, a["id"], "rev1")
    assert admin_cli.reviewer_transition([], sm) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "dry-run"
    assert {(x["subject"], x["role"]) for x in report["legacy_assignments"]} == {
        (SUBJECTS["rev1"], "reviewer"), (SUBJECTS["rev2"], "reviewer"), (SUBJECTS["appr"], "approver")}
    assert [x["id"] for x in report["open_applications_held_by_non_admin_reviewers"]] == [a["id"]]
    with sm() as db:                                                  # dry run changed nothing
        assert db.scalar(select(RoleAssignment).where(RoleAssignment.role == "reviewer",
                                                      RoleAssignment.active.is_(False))) is None
    assert admin_cli.reviewer_transition(["--apply", "--actor", "ops:test", "--reason", "retired"], sm) == 0
    applied = json.loads(capsys.readouterr().out)
    assert len(applied["revoked"]) == 3
    with sm() as db:
        revokes = db.scalars(select(RoleEvent).where(RoleEvent.action == "revoke")).all()
        assert {r.subject for r in revokes} == {SUBJECTS["rev1"], SUBJECTS["rev2"], SUBJECTS["appr"]}
        app_row = db.get(LoanApplication, a["id"])
        assert app_row.reviewer_subject == SUBJECTS["rev1"] and app_row.status == "under_review"   # untouched
        assert db.scalar(select(RoleAssignment).where(RoleAssignment.subject == SUBJECTS["desk"])).active is True
