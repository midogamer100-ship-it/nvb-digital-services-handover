"""Extension role assignments bound to the core's stable subject (``nvb-user:<id>:<created>``).

Core roles (customer/admin/demo) grant NOTHING here: a staff capability exists only as an
explicit, audited assignment row in the owning service's own database. Each service declares
which roles it knows. Assignments are per service: least privilege, no shared mutable store.
"""

from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from nvb_platform.db import utcnow

SUBJECT_RE = re.compile(r"^nvb-user:\d{1,12}:\d{1,12}$")
_PK = BigInteger().with_variant(Integer, "sqlite")


class RoleError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code, self.code, self.message = status_code, code, message


class RoleAssignmentMixin:
    __tablename__ = "role_assignments"
    __table_args__ = (UniqueConstraint("subject", "role", name="uq_role_subject_role"),)

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    subject: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    label: Mapped[str] = mapped_column(String(80), nullable=False, default="")  # display only (username)
    updated_by: Mapped[str] = mapped_column(String(80), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class RoleEventMixin:
    """Append-only history of grants/revocations (application role: INSERT/SELECT only)."""

    __tablename__ = "role_events"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    subject: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(8), nullable=False)  # grant | revoke
    actor: Mapped[str] = mapped_column(String(80), nullable=False)
    reason: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


def active_roles(db: Session, assignment_model, subject: str) -> frozenset[str]:
    rows = db.scalars(select(assignment_model.role).where(assignment_model.subject == subject,
                                                          assignment_model.active.is_(True))).all()
    return frozenset(rows)


def list_assignments(db: Session, assignment_model) -> list[dict]:
    rows = db.scalars(select(assignment_model).where(assignment_model.active.is_(True))
                      .order_by(assignment_model.subject, assignment_model.role)).all()
    return [{"subject": r.subject, "role": r.role, "label": r.label, "updated_by": r.updated_by,
             "updated_at": r.updated_at.isoformat() if r.updated_at else None} for r in rows]


def set_role(db: Session, assignment_model, event_model, *, subject: str, role: str, active: bool, actor: str,
             known_roles: frozenset[str], reason: str = "", label: str = "") -> bool:
    """Grant or revoke. Returns True when the stored state changed. Caller commits."""
    if not SUBJECT_RE.match(subject):
        raise RoleError(422, "invalid_subject", "Subject is not valid.")
    if role not in known_roles:
        raise RoleError(422, "unknown_role", f"Unknown role. Known roles: {sorted(known_roles)}.")
    row = db.scalar(select(assignment_model).where(assignment_model.subject == subject,
                                                   assignment_model.role == role).with_for_update())
    now = utcnow()
    if row is None:
        if not active:
            return False
        db.add(assignment_model(subject=subject, role=role, active=True, label=label[:80], updated_by=actor[:80],
                                updated_at=now))
    elif row.active == active:
        return False
    else:
        row.active, row.updated_by, row.updated_at = active, actor[:80], now
        if label:
            row.label = label[:80]
    db.add(event_model(subject=subject, role=role, action="grant" if active else "revoke", actor=actor[:80],
                       reason=reason[:200], created_at=now))
    return True
