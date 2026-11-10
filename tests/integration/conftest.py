"""Integration tests against REAL, DISPOSABLE PostgreSQL, RabbitMQ and S3-compatible storage.

Safety (every check fails closed and happens before anything is written):

* skipped entirely unless ``NVB_DIGITAL_TEST_ENV=disposable``;
* database hosts must be loopback or listed in ``NVB_DIGITAL_TEST_ALLOW_HOSTS``;
* database names must start with ``nvb_disposable_test_``;
* each database must carry the comment ``nvb-disposable-test`` (set by whoever provisioned it);
* the roles must not be superusers.

Run through ``scripts/run_integration_tests.py`` (loads the endpoints of the local disposable
stack or an env file, strips inherited NVB_/ADAPTER_/STATEMENTS_/LOANS_ variables).
Credentials are read from files; nothing here prints them.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import pytest

REPO = Path(__file__).resolve().parents[2]
CORE_DIR = os.environ.get("CORE_BACKEND_DIR")
MARKER = "nvb-disposable-test"
PREFIX = "nvb_disposable_test_"
for extra in ("libs/nvb_platform", "services/core-adapter", "services/statements", "services/loans"):
    path = str(REPO / extra)
    if path not in sys.path:
        sys.path.insert(0, path)

REQUIRED = ("TEST_STATEMENTS_DATABASE_URL", "TEST_STATEMENTS_DATABASE_PASSWORD_FILE", "TEST_BROKER_URL",
            "TEST_BROKER_USERNAME_FILE", "TEST_BROKER_PASSWORD_FILE", "TEST_S3_ENDPOINT_URL", "TEST_S3_BUCKET",
            "TEST_S3_ACCESS_KEY_FILE", "TEST_S3_SECRET_KEY_FILE")


def _enabled() -> str | None:
    if os.environ.get("NVB_DIGITAL_TEST_ENV") != "disposable":
        return "NVB_DIGITAL_TEST_ENV=disposable is not set (run scripts/run_integration_tests.py)"
    missing = [k for k in REQUIRED if not os.environ.get(k)]
    return f"missing settings: {missing}" if missing else None


def pytest_collection_modifyitems(config, items):
    reason = _enabled()
    if reason:
        skip = pytest.mark.skip(reason=reason)
        for item in items:
            item.add_marker(skip)


def read(path_env: str) -> str:
    return Path(os.environ[path_env]).read_text(encoding="utf-8").strip()


def verify_disposable_database(url_env: str, password_env: str) -> dict:
    """Prove the target is an explicitly provisioned disposable database; return connect kwargs."""
    import psycopg

    parts = urlsplit(os.environ[url_env])
    allowed = {"127.0.0.1", "localhost", "::1"} | {h.strip().lower() for h in
                                                   os.environ.get("NVB_DIGITAL_TEST_ALLOW_HOSTS", "").split(",") if h}
    dbname = parts.path.lstrip("/")
    if (parts.hostname or "").lower() not in allowed:
        pytest.exit(f"Refused: {url_env} host is not in the disposable allowlist.", returncode=3)
    if not dbname.startswith(PREFIX):
        pytest.exit(f"Refused: {url_env} database name must start with {PREFIX!r}.", returncode=3)
    if parts.password:
        pytest.exit(f"Refused: {url_env} must not embed a password.", returncode=3)
    kw = dict(host=parts.hostname, port=parts.port or 5432, dbname=dbname, user=parts.username,
              password=read(password_env), connect_timeout=5)
    with psycopg.connect(**kw) as conn:
        db, is_super, marker = conn.execute(
            "SELECT current_database(), (SELECT rolsuper FROM pg_roles WHERE rolname = current_user), "
            "(SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = current_database())"
        ).fetchone()
    if db != dbname or is_super or marker != MARKER:
        pytest.exit(f"Refused: {url_env} is not a marked disposable database / role is a superuser.", returncode=3)
    return kw


def run_migration(service: str, owner_url: str, owner_password_file: str, app_role: str | None,
                  revision: str = "head") -> None:
    """Run the real alembic command exactly as the migration Job does (subprocess, sanitized env)."""
    prefix = service.upper()
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("NVB_", "ADAPTER_", "STATEMENTS_", "LOANS_", "GATEWAY_", "PG"))}
    env.update({f"{prefix}_MIGRATION_DATABASE_URL": owner_url, f"{prefix}_MIGRATION_PASSWORD_FILE": owner_password_file,
                "PYTHONPATH": os.pathsep.join([str(REPO / "libs" / "nvb_platform"),
                                               str(REPO / "services" / service)])})
    if app_role:
        env[f"{prefix}_APP_ROLE"] = app_role
    out = subprocess.run([sys.executable, "-m", "alembic", "-c", str(REPO / "migrations" / service / "alembic.ini"),
                          "upgrade", revision], env=env, capture_output=True, text=True, cwd=str(REPO))
    assert out.returncode == 0, out.stdout[-2000:] + out.stderr[-2000:]


def reset_schema(owner_kw: dict) -> None:
    import psycopg

    with psycopg.connect(**owner_kw, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT USAGE ON SCHEMA public TO PUBLIC")


# --- statements database ------------------------------------------------------------------


@pytest.fixture(scope="session")
def stm_owner_kw():
    return verify_disposable_database("TEST_STATEMENTS_DATABASE_URL", "TEST_STATEMENTS_DATABASE_PASSWORD_FILE")


@pytest.fixture(scope="session")
def stm_migrated(stm_owner_kw):
    """Fresh schema migrated to head by the real migration command, with app-role grants."""
    reset_schema(stm_owner_kw)
    run_migration("statements", os.environ["TEST_STATEMENTS_DATABASE_URL"],
                  os.environ["TEST_STATEMENTS_DATABASE_PASSWORD_FILE"], os.environ.get("TEST_STATEMENTS_APP_ROLE"))
    return True


@pytest.fixture
def stm_clean(stm_owner_kw, stm_migrated):
    import psycopg

    with psycopg.connect(**stm_owner_kw, autocommit=True) as conn:
        conn.execute("TRUNCATE statement_requests, statement_events, outbox_messages, statement_batches, "
                     "batch_items, role_assignments, role_events RESTART IDENTITY")


def app_database_url(service: str) -> tuple[str, str]:
    """(URL, password file) of the least-privilege runtime role, falling back to the owner."""
    up = service.upper()
    owner = urlsplit(os.environ[f"TEST_{up}_DATABASE_URL"])
    role = os.environ.get(f"TEST_{up}_APP_ROLE")
    if not role:
        return os.environ[f"TEST_{up}_DATABASE_URL"], os.environ[f"TEST_{up}_DATABASE_PASSWORD_FILE"]
    return owner._replace(netloc=f"{role}@{owner.hostname}:{owner.port or 5432}").geturl(), \
        os.environ[f"TEST_{up}_APP_PASSWORD_FILE"]


@pytest.fixture
def queues():
    """Unique queue names per test inside the disposable vhost; deleted afterwards."""
    tag = uuid.uuid4().hex[:10]
    names = {"interactive": f"nvb.test.{tag}.generate", "batch": f"nvb.test.{tag}.batch"}
    yield names
    try:
        import pika

        from nvb_platform.broker import RabbitBroker

        b = RabbitBroker(_BrokerSettings())
        for q in names.values():
            for name in (q, f"{q}.dead"):
                try:
                    b._ch.queue_delete(name)
                except pika.exceptions.AMQPError:
                    b._ch = b._conn.channel()
        b.close()
    except Exception:  # noqa: BLE001 - cleanup only
        pass


class _BrokerSettings:
    broker_url = os.environ.get("TEST_BROKER_URL")
    broker_username_file = os.environ.get("TEST_BROKER_USERNAME_FILE")
    broker_password_file = os.environ.get("TEST_BROKER_PASSWORD_FILE")
    broker_ca_file = os.environ.get("TEST_BROKER_CA_FILE")
    broker_heartbeat_seconds = 60


@pytest.fixture
def broker_settings():
    return _BrokerSettings()


@pytest.fixture
def stm_settings(stm_clean, queues, tmp_path):
    from nvb_statements.config import Settings

    url, pw_file = app_database_url("statements")
    return Settings(
        database_url=url, database_password_file=pw_file,
        broker_url=os.environ["TEST_BROKER_URL"], broker_username_file=os.environ["TEST_BROKER_USERNAME_FILE"],
        broker_password_file=os.environ["TEST_BROKER_PASSWORD_FILE"], broker_ca_file=os.environ.get("TEST_BROKER_CA_FILE"),
        allow_plaintext_dependencies=os.environ["TEST_BROKER_URL"].startswith("amqp://"),
        queue_name=queues["interactive"], batch_queue_name=queues["batch"],
        s3_endpoint_url=os.environ["TEST_S3_ENDPOINT_URL"], s3_bucket=os.environ["TEST_S3_BUCKET"],
        s3_access_key_file=os.environ["TEST_S3_ACCESS_KEY_FILE"], s3_secret_key_file=os.environ["TEST_S3_SECRET_KEY_FILE"],
        s3_ca_file=os.environ.get("TEST_S3_CA_FILE"),
        adapter_base_url="http://adapter.inprocess", allow_insecure_http=True,
        settle_seconds=600, retry_base_seconds=0, retry_max_seconds=0, max_attempts=3, entries_page_size=50,
        idle_exit_seconds=1, lease_seconds=30, log_level="WARNING", broker_heartbeat_seconds=60)


@pytest.fixture
def stm_sm(stm_settings):
    from nvb_statements.db import build_engine, build_sessionmaker

    engine = build_engine(stm_settings, "nvb-integration-tests")
    yield build_sessionmaker(engine)
    engine.dispose()


# --- in-process core (on its own disposable PostgreSQL database) + adapter -----------------------

PASSWORDS = {"customer": "customer-pass-123", "loadtest": "loadtest-pass-123",
             "demo": "demo-pass-123456", "admin": "admin-pass-123456"}


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@pytest.fixture
def core_stack(tmp_path, monkeypatch):
    """Patched core (PostgreSQL, REPEATABLE READ exports) + real Core Adapter, in-process ASGI."""
    if not CORE_DIR or not os.environ.get("TEST_CORE_DATABASE_URL"):
        pytest.skip("CORE_BACKEND_DIR / TEST_CORE_DATABASE_URL not set")
    import httpx
    from fastapi.testclient import TestClient

    owner_kw = verify_disposable_database("TEST_CORE_DATABASE_URL", "TEST_CORE_DATABASE_PASSWORD_FILE")
    reset_schema(owner_kw)
    monkeypatch.syspath_prepend(CORE_DIR)
    from app.config import Settings as CoreSettings
    from app.db import Base as CoreBase
    from app.main import create_app as create_core
    from app.services.seed import seed_database
    from core_adapter import credentials
    from core_adapter.app import create_app as create_adapter
    from core_adapter.config import Settings as AdapterSettings
    from sqlalchemy.engine import make_url

    monkeypatch.setattr(credentials, "RELOAD_CHECK_SECONDS", 0)
    core_tok = secrets.token_urlsafe(32)
    tokens = {name: secrets.token_urlsafe(32) for name in
              ("statements-api", "statements-worker", "loans-api", "loans-worker")}
    (tmp_path / "core-clients.json").write_text(json.dumps({"clients": [{
        "client_id": "nvb-core-adapter", "token_sha256": [_digest(core_tok)],
        "scopes": ["identity:introspect", "accounts:read", "statements:extract", "summaries:read",
                   "accounts:discover"]}]}))
    core_url = make_url(os.environ["TEST_CORE_DATABASE_URL"]).set(password=read("TEST_CORE_DATABASE_PASSWORD_FILE"))
    core = create_core(CoreSettings(
        database_url=core_url.render_as_string(hide_password=False), cookie_secure=False, argon2_time_cost=1,
        argon2_memory_cost_kib=1024, log_level="WARNING", integration_enabled=True,
        integration_clients_file=str(tmp_path / "core-clients.json"), integration_settle_seconds=300))
    CoreBase.metadata.create_all(core.state.engine)
    db = core.state.sessionmaker()
    seed_database(db, core.state.passwords, PASSWORDS, customer_count=3, loadtest_count=0, demo_count=0)
    db.close()

    (tmp_path / "core-token").write_text(core_tok)
    scopes = {"statements-api": ["identity:resolve", "accounts:read", "discovery:read"],
              "statements-worker": ["statements:read"],
              "loans-api": ["identity:resolve", "accounts:read"],
              "loans-worker": ["summaries:read", "accounts:read"]}
    (tmp_path / "adapter-clients.json").write_text(json.dumps({"clients": [
        {"client_id": name, "token_sha256": [_digest(tok)], "scopes": scopes[name]} for name, tok in tokens.items()]}))
    adapter = create_adapter(AdapterSettings(
        core_base_url="http://core.inprocess", allow_insecure_http=True, core_token_file=str(tmp_path / "core-token"),
        clients_file=str(tmp_path / "adapter-clients.json"), log_level="WARNING"),
        core_transport=httpx.ASGITransport(app=core))
    token_files = {}
    for name, tok in tokens.items():
        token_files[name] = str(tmp_path / f"{name}-token")
        Path(token_files[name]).write_text(tok)

    def login(username: str, group: str = "customer"):
        c = TestClient(core)
        r = c.post("/api/auth/login", json={"username": username, "password": PASSWORDS[group]})
        assert r.status_code == 200, r.text
        return c, c.cookies.get("nvb_session"), r.json()["csrf_token"]

    yield {"core": core, "adapter": adapter, "adapter_transport": TestClient(adapter)._transport,
           "token_files": token_files, "login": login}
    core.state.engine.dispose()
