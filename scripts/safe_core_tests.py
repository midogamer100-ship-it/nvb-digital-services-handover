"""Run the NVB core (monolith) pytest suite with a sanitized environment.

Why this exists: backend/tests/conftest.py in the core executes
``DROP SCHEMA public CASCADE; CREATE SCHEMA public`` when NVB_TEST_DATABASE_URL
points at PostgreSQL. Pointing it at the core or shared demo database destroys it.

Behaviour:
* Every inherited ``NVB_*`` variable is removed from the child environment.
* By default the suite runs on a temporary SQLite file (NVB_TEST_DATABASE_URL unset).
* PostgreSQL is used only with ``--postgres-url`` AND ``--i-confirm-disposable``, and
  only when the host is in an explicit allowlist (default: localhost / 127.0.0.1 / ::1).
  The database name must start with ``nvb_disposable_test_``. A database name alone is
  not proof of safety, so the host allowlist is also required.
* Secret values are never printed (the URL is shown with the password redacted).

Usage:
    python scripts/safe_core_tests.py --core-dir ../NVB-Monolith-integration/backend [-- pytest args]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1"}
REQUIRED_DB_PREFIX = "nvb_disposable_test_"


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--core-dir", required=True, type=Path)
    parser.add_argument("--postgres-url")
    parser.add_argument("--i-confirm-disposable", action="store_true")
    parser.add_argument("--allow-host", action="append", default=[])
    parser.add_argument("pytest_args", nargs="*")
    args = parser.parse_args()

    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("NVB_")}
    removed = sorted(k for k in os.environ if k.upper().startswith("NVB_"))
    if removed:
        print(f"Removed inherited variables (values not shown): {removed}")

    if args.postgres_url:
        if not args.i_confirm_disposable:
            raise SystemExit("Refused: PostgreSQL runs DROP SCHEMA public CASCADE. Pass --i-confirm-disposable.")
        check_postgres_target(args.postgres_url, DEFAULT_ALLOWED_HOSTS | {h.lower() for h in args.allow_host})
        env["NVB_TEST_DATABASE_URL"] = args.postgres_url
        print(f"Target: disposable PostgreSQL {redact(args.postgres_url)}")
    else:
        print("Target: temporary SQLite per test (no PostgreSQL). SQLite does not prove PG locking semantics.")

    cmd = [sys.executable, "-m", "pytest", *args.pytest_args]
    return subprocess.call(cmd, cwd=args.core_dir, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
