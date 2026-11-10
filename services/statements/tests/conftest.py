from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from nvb_statements.api import create_app
from nvb_statements.broker import InMemoryBroker
from nvb_statements.config import Settings
from nvb_statements.db import build_engine, build_sessionmaker
from nvb_statements.models import Base
from nvb_statements.storage import InMemoryStore
from nvb_statements.worker import Worker
from tests.fakes import FakeAdapter, account

A_SUBJECT = "nvb-user:1:1700000000"
B_SUBJECT = "nvb-user:2:1700000001"
SEPT_START = datetime(2026, 8, 31, 21, 0, tzinfo=UTC)   # 2026-09-01 00:00 Africa/Cairo (EEST, UTC+3)
SEPT_END = datetime(2026, 9, 30, 21, 0, tzinfo=UTC)     # 2026-10-01 00:00 Africa/Cairo


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in list(os.environ):
        if k.upper().startswith(("STATEMENTS_", "NVB_")):
            monkeypatch.delenv(k)


@pytest.fixture
def settings(tmp_path):
    return Settings(database_url=f"sqlite:///{tmp_path / 'statements.db'}", settle_seconds=0, log_level="WARNING",
                    retry_base_seconds=0, retry_max_seconds=0, max_attempts=3, entries_page_size=50,
                    idle_exit_seconds=0, lease_seconds=60)


@pytest.fixture
def sm(settings):
    engine = build_engine(settings)
    Base.metadata.create_all(engine)
    yield build_sessionmaker(engine)
    engine.dispose()


@pytest.fixture
def fake():
    f = FakeAdapter()
    f.a_token, f.a_csrf = f.add_customer(A_SUBJECT, [account(11, "1111"), account(12, "1212")])
    f.b_token, f.b_csrf = f.add_customer(B_SUBJECT, [account(21, "2121")])
    f.admin_token, f.admin_csrf = f.add_customer("nvb-user:3:1700000002", [], role="admin")
    f.post(11, datetime(2026, 8, 15, tzinfo=UTC), "credit", 100_000)          # before period -> opening
    f.post(11, SEPT_START, "credit", 50_050)                                     # exactly at start (included)
    f.post(11, datetime(2026, 9, 10, 12, tzinfo=UTC), "debit", 12_345)
    f.post(11, datetime(2026, 9, 10, 12, tzinfo=UTC), "debit", 1)               # tied timestamp
    f.post(11, SEPT_END, "credit", 99_999)                                       # exactly at end (excluded)
    return f


@pytest.fixture
def store():
    return InMemoryStore()


@pytest.fixture
def broker():
    return InMemoryBroker()


@pytest.fixture
def app(settings, sm, fake, store):
    return create_app(settings, adapter=fake, store=store, sessionmaker=sm)


def browser(app, fake, who="a") -> TestClient:
    c = TestClient(app)
    c.cookies.set("nvb_session", getattr(fake, f"{who}_token"))
    c.headers["X-CSRF-Token"] = getattr(fake, f"{who}_csrf")
    return c


@pytest.fixture
def worker(settings, sm, broker, store, fake):
    return Worker(settings, sm, broker, store, fake, worker_id="w1")
