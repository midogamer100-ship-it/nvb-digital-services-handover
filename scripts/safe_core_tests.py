"""Run the NVB core (monolith) pytest suite with a sanitized environment.

Why this exists: backend/tests/conftest.py in the core executes
``DROP SCHEMA public CASCADE; CREATE SCHEMA public`` when NVB_TEST_DATABASE_URL
points at PostgreSQL. Pointing it at the core or shared demo database destroys it.

Behaviour:
* Every inherited ``NVB_*``, ``ADAPTER_*``, ``STATEMENTS_*``, ``LOANS_*``, ``GATEWAY_*`` and libpq
  ``PG*`` variable is removed from the child environment.
* By default the suite runs on a temporary SQLite file (NVB_TEST_DATABASE_URL unset).
* PostgreSQL is used only with ``--postgres-url`` AND ``--i-confirm-disposable``, and
  only when the host is in an explicit allowlist (default: localhost / 127.0.0.1 / ::1).
  The database name must start with ``nvb_disposable_test_``. A database name alone is
  not proof of safety, so the host allowlist is also required.
* Before pytest starts, the runner CONNECTS and verifies the live target: ``current_database()``
  matches, the role is not a superuser, and the database carries the comment
  ``nvb-disposable-test`` (set explicitly by whoever provisioned it, e.g.
  ``COMMENT ON DATABASE nvb_disposable_test_core IS 'nvb-disposable-test'``). A database that
  was not deliberately marked disposable is refused even if its name and host look right.
* The password is read from ``--postgres-password-file`` (preferred; keeps it off the command
  line). Secret values are never printed (the URL is shown with the password redacted).

Usage:
    python scripts/safe_core_tests.py --core-dir ../NVB-Monolith-integration/backend [-- pytest args]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit

DEFAULT_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1"}
REQUIRED_DB_PREFIX = "nvb_disposable_test_"
DISPOSABLE_MARKER = "nvb-disposable-test"
STRIP_PREFIXES = ("NVB_", "ADAPTER_", "STATEMENTS_", "LOANS_", "GATEWAY_", "PG")


def redact(url: str) -> str:
    parts = urlsplit(url)
    if parts.password:
        netloc = parts.netloc.replace(":" + parts.password + "@", ":***@")
        return parts._replace(netloc=netloc).geturl()
    return url


def check_postgres_target(url: str, allowed_hosts: set[str]) -> None:
    parts = urlsplit(url)
    if not parts.scheme.startswith("postgresql"):
        raise SystemExit("Refused: --postgres-url must be a postgresql URL.")
    host = (parts.hostname or "").lower()
    if host not in allowed_hosts:
        raise SystemExit(f"Refused: host {host!r} is not in the disposable allowlist {sorted(allowed_hosts)}.")
    db = parts.path.lstrip("/")
    if not db.startswith(REQUIRED_DB_PREFIX):
        raise SystemExit(f"Refused: database name must start with {REQUIRED_DB_PREFIX!r}.")


def verify_live_target(url: str) -> None:
    """Connect and prove the target was explicitly provisioned as disposable. Fails closed."""
    import psycopg

    parts = urlsplit(url)
    expected_db = parts.path.lstrip("/")
    try:
        with psycopg.connect(host=parts.hostname, port=parts.port or 5432, dbname=expected_db,
                             user=parts.username, password=parts.password, connect_timeout=5) as conn:
            db, is_super, marker = conn.execute(
                "SELECT current_database(), (SELECT rolsuper FROM pg_roles WHERE rolname = current_user), "
                "(SELECT shobj_description(oid, 'pg_database') FROM pg_database "
                " WHERE datname = current_database())").fetchone()
    except psycopg.Error as exc:
        raise SystemExit(f"Refused: cannot verify the target ({type(exc).__name__}).") from None
    if db != expected_db:
        raise SystemExit("Refused: connected database differs from the requested one.")
    if is_super:
        raise SystemExit("Refused: the test role is a superuser; use a role limited to the disposable database.")
    if marker != DISPOSABLE_MARKER:
        raise SystemExit(f"Refused: database {db!r} is not marked disposable "
                         f"(expected database comment {DISPOSABLE_MARKER!r}).")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--core-dir", required=True, type=Path)
    parser.add_argument("--postgres-url", help="postgresql+psycopg://user@host:port/nvb_disposable_test_...")
    parser.add_argument("--postgres-password-file", type=Path)
    parser.add_argument("--i-confirm-disposable", action="store_true")
    parser.add_argument("--allow-host", action="append", default=[])
    parser.add_argument("pytest_args", nargs="*")
    args = parser.parse_args()

    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(STRIP_PREFIXES)}
    removed = sorted(k for k in os.environ if k.upper().startswith(STRIP_PREFIXES))
    if removed:
        print(f"Removed inherited variables (values not shown): {removed}")

    if args.postgres_url:
        if not args.i_confirm_disposable:
            raise SystemExit("Refused: PostgreSQL runs DROP SCHEMA public CASCADE. Pass --i-confirm-disposable.")
        url = args.postgres_url
        if args.postgres_password_file:
            parts = urlsplit(url)
            if parts.password:
                raise SystemExit("Refused: give the password in the URL or in the file, not both.")
            password = quote(args.postgres_password_file.read_text(encoding="utf-8").strip(), safe="")
            url = parts._replace(netloc=parts.netloc.replace("@", f":{password}@", 1)).geturl()
        check_postgres_target(url, DEFAULT_ALLOWED_HOSTS | {h.lower() for h in args.allow_host})
        verify_live_target(url)
        env["NVB_TEST_DATABASE_URL"] = url
        print(f"Target: disposable PostgreSQL {redact(url)} (marker verified)")
    else:
        print("Target: temporary SQLite per test (no PostgreSQL). SQLite does not prove PG locking semantics.")

    cmd = [sys.executable, "-m", "pytest", *args.pytest_args]
    return subprocess.call(cmd, cwd=args.core_dir, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
