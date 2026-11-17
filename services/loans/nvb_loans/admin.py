"""Role administration command for the loans service.

    python -m nvb_loans.admin grant --subject nvb-user:12:1730000000 --role operations_admin \
        --actor "ops:<ticket-or-name>" --reason "loan desk"
    python -m nvb_loans.admin list
    python -m nvb_loans.admin reviewer-transition                      # dry-run report (default)
    python -m nvb_loans.admin reviewer-transition --apply --actor "ops:<ticket>" --reason "..."

Roles of this service: operations_admin, batch_operator, extension_admin, and the retired
reviewer / approver (effective only with LOANS_LEGACY_REVIEWER_WORKFLOW_ENABLED=true).
See nvb_platform.role_cli.

``reviewer-transition`` retires the separate reviewer workflow explicitly:
* the dry run lists active reviewer/approver assignments and the open applications held by a
  reviewer who is not an Operations Admin (the Operations Admin takes those over with
  "start review"; nothing is reassigned automatically);
* ``--apply`` revokes the reviewer and approver assignments through the normal audited path
  (one ``role_events`` row per revocation). It never changes applications, their history,
  recommendations or any customer data, and never grants anything. Re-grant to roll back.
"""

from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import select

from nvb_loans.config import get_settings
from nvb_loans.db import build_engine
from nvb_loans.models import KNOWN_ROLES, LoanApplication, RoleAssignment, RoleEvent
from nvb_platform import roles as roles_lib
from nvb_platform.db import ConfigError, build_sessionmaker
from nvb_platform.role_cli import run


def transition_report(db) -> dict:
    legacy = db.scalars(select(RoleAssignment).where(RoleAssignment.role.in_(sorted(roles_lib.LEGACY_REVIEW_ROLES)),
                                                     RoleAssignment.active.is_(True))
                        .order_by(RoleAssignment.subject, RoleAssignment.role)).all()
    admins = set(db.scalars(select(RoleAssignment.subject).where(
        RoleAssignment.role == roles_lib.OPERATIONS_ADMIN, RoleAssignment.active.is_(True))).all())
    held = db.scalars(select(LoanApplication).where(LoanApplication.status.in_(("submitted", "under_review")),
                                                    LoanApplication.reviewer_subject.is_not(None))
                      .order_by(LoanApplication.submitted_at)).all()
    return {
        "legacy_assignments": [{"subject": r.subject, "role": r.role, "label": r.label} for r in legacy],
        "open_applications_held_by_non_admin_reviewers": [
            {"id": a.id, "status": a.status, "reviewer_subject": a.reviewer_subject,
             "has_recommendation": a.recommendation is not None}
            for a in held if a.reviewer_subject not in admins],
        "note": "Applications are never modified by this command. An Operations Admin takes over "
                "unrecommended ones with 'start review' and decides recommended ones directly.",
    }


def reviewer_transition(argv: list[str], sessionmaker) -> int:
    p = argparse.ArgumentParser(prog="loans-admin reviewer-transition")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True)
    mode.add_argument("--apply", action="store_true")
    p.add_argument("--actor", help="who performs the change, e.g. ops:<ticket>")
    p.add_argument("--reason", default="")
    args = p.parse_args(argv)
    if args.apply and not args.actor:
        p.error("--apply requires --actor")
    with sessionmaker() as db:
        report = transition_report(db)
        if not args.apply:
            print(json.dumps({"mode": "dry-run", **report}, indent=2))
            return 0
        revoked = []
        for item in report["legacy_assignments"]:
            if roles_lib.set_role(db, RoleAssignment, RoleEvent, subject=item["subject"], role=item["role"],
                                  active=False, actor=args.actor, known_roles=KNOWN_ROLES,
                                  reason=(args.reason or "reviewer workflow retired")[:200]):
                revoked.append({"subject": item["subject"], "role": item["role"]})
        db.commit()
        print(json.dumps({"mode": "apply", "revoked": revoked,
                          "open_applications_held_by_non_admin_reviewers":
                              report["open_applications_held_by_non_admin_reviewers"]}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    settings = get_settings()
    if argv[:1] == ["reviewer-transition"]:
        try:
            sm = build_sessionmaker(build_engine(settings, "nvb-loans-admin"))
        except ConfigError as exc:
            print(f"configuration error: {exc}", file=sys.stderr)
            return 78
        return reviewer_transition(argv[1:], sm)
    return run(argv, settings=settings, known_roles=KNOWN_ROLES, assignment_model=RoleAssignment,
               event_model=RoleEvent, engine_name="nvb-loans-admin", prog="loans-admin", build_engine=build_engine)


if __name__ == "__main__":
    sys.exit(main())
