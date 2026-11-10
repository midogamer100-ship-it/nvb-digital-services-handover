from __future__ import annotations

import os
import secrets

import pytest
from fastapi.testclient import TestClient

from nvb_loans.api import create_app
from nvb_loans.config import Settings
from nvb_loans.db import build_engine, build_sessionmaker
from nvb_loans.models import KNOWN_ROLES, Base, RoleAssignment, RoleEvent
from nvb_loans.worker import Worker
from nvb_platform import roles as roles_lib
from nvb_platform.adapter_client import AdapterCallError, Identity
from nvb_platform.broker import InMemoryBroker

SUBJECTS = {"alice": "nvb-user:1:1700000000", "bob": "nvb-user:2:1700000001", "rev1": "nvb-user:11:1700000011",
            "rev2": "nvb-user:12:1700000012", "appr": "nvb-user:13:1700000013", "admin": "nvb-user:14:1700000014",
            "ops": "nvb-user:15:1700000015"}


def account(account_id: int) -> dict:
    return {"account_id": account_id, "masked_number": f"•••• {account_id:04d}", "type": "current",
            "nickname": "Everyday", "currency": "EGP", "status": "active", "opened_at": "2026-01-01T09:00:00+00:00"}


class FakeAdapter:
    """In-memory Core Adapter double: sessions, account ownership and manifest totals."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}
        self.owner: dict[int, str] = {}
        self.unavailable = False
        self.manifest_calls: list[tuple] = []

    def add(self, name: str, role: str, accounts: list[dict]) -> None:
        token, csrf = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
        self.sessions[token] = {"subject": SUBJECTS[name], "name": name, "role": role, "csrf": csrf,
                                "accounts": accounts}
        for a in accounts:
            self.owner[a["account_id"]] = SUBJECTS[name]
        setattr(self, f"{name}_token", token)
        setattr(self, f"{name}_csrf", csrf)

    def close(self) -> None:
        pass

    def resolve(self, session_token, csrf_token, request_id, include_accounts=True):
        if self.unavailable:
            raise AdapterCallError(503, "core_unavailable", "down", True)
        s = self.sessions.get(session_token)
        if s is None:
            return None
        return Identity(subject=s["subject"], role=s["role"], username=s["name"], display_name=s["name"],
                        csrf_valid=None if csrf_token is None else csrf_token == s["csrf"],
                        accounts=list(s["accounts"]) if s["role"] == "customer" else [])

    def manifest(self, account_id, subject, start, end, request_id=None):
        if self.unavailable:
            raise AdapterCallError(503, "core_timeout", "down", True)
        if self.owner.get(account_id) != subject:
            raise AdapterCallError(404, "not_found", "Not found.", False)
        self.manifest_calls.append((account_id, start, end))
        return {"credit_total_minor": 900_000, "debit_total_minor": 600_000, "closing_balance_minor": 1_250_000,
                "entry_count": 12, "dataset_version": "statement-v2"}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in list(os.environ):
        if k.upper().startswith(("LOANS_", "STATEMENTS_", "NVB_", "ADAPTER_", "KUBERNETES_")):
            monkeypatch.delenv(k)


@pytest.fixture
def settings(tmp_path):
    return Settings(database_url=f"sqlite:///{tmp_path / 'loans.db'}", allow_sqlite=True, log_level="WARNING",
                    settle_seconds=0, retry_base_seconds=0, retry_max_seconds=0, max_attempts=3, idle_exit_seconds=0)


@pytest.fixture
def sm(settings):
    engine = build_engine(settings)
    Base.metadata.create_all(engine)
    yield build_sessionmaker(engine)
    engine.dispose()


@pytest.fixture
def fake():
    f = FakeAdapter()
    f.add("alice", "customer", [account(101), account(102)])
    f.add("bob", "customer", [account(201)])
    for staff in ("rev1", "rev2", "appr", "ops"):
        f.add(staff, "admin", [])
    f.add("admin", "admin", [])
    return f


def grant(sm, name: str, role: str) -> None:
    with sm() as db:
        roles_lib.set_role(db, RoleAssignment, RoleEvent, subject=SUBJECTS[name], role=role, active=True,
                           actor="test", known_roles=KNOWN_ROLES, label=name)
        db.commit()


@pytest.fixture
def roles(sm):
    grant(sm, "rev1", "reviewer")
    grant(sm, "rev2", "reviewer")
    grant(sm, "appr", "approver")
    grant(sm, "admin", "extension_admin")
    grant(sm, "ops", "batch_operator")


@pytest.fixture
def app(settings, sm, fake):
    return create_app(settings, adapter=fake, sessionmaker=sm)


def browser(app, fake, who: str) -> TestClient:
    c = TestClient(app)
    c.cookies.set("nvb_session", getattr(fake, f"{who}_token"))
    c.headers["X-CSRF-Token"] = getattr(fake, f"{who}_csrf")
    return c


@pytest.fixture
def broker():
    return InMemoryBroker()


@pytest.fixture
def worker(settings, sm, broker, fake):
    return Worker(settings, sm, broker, fake, worker_id="w1")
