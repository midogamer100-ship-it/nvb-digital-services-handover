"""Run tests/integration against DISPOSABLE PostgreSQL, RabbitMQ and S3 only.

Endpoints come from the local disposable stack (``scripts/local_stack.py``, default) or from
``--env-file`` (KEY=VALUE lines naming URLs and secret FILE paths - never secret values).
Every inherited NVB_/ADAPTER_/STATEMENTS_/LOANS_/GATEWAY_/PG*/AWS_* variable is removed first, so
a shell that happens to point at a shared environment cannot leak into the tests. The tests
themselves re-verify each database (host allowlist, name prefix, disposable marker, no superuser).

    python scripts/run_integration_tests.py                       # local stack must be started
    python scripts/run_integration_tests.py --env-file infra.env --allow-host pg.test.example
    python scripts/run_integration_tests.py -- -k rabbit -x      # extra pytest arguments
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STRIP = ("NVB_", "ADAPTER_", "STATEMENTS_", "LOANS_", "GATEWAY_", "PG", "AWS_", "TEST_", "KUBERNETES_")
SECRET_HINTS = ("PASSWORD", "TOKEN", "SECRET", "KEY")


def load_env_file(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env-file", type=Path)
    p.add_argument("--allow-host", action="append", default=[])
    p.add_argument("--core-dir", type=Path, default=ROOT.parent / "NVB-Monolith-integration" / "backend")
    p.add_argument("pytest_args", nargs="*")
    args = p.parse_args()

    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(STRIP)}
    removed = sorted(k for k in os.environ if k.upper().startswith(STRIP))
    if removed:
        print(f"Removed inherited variables (values not shown): {removed}")
    if args.env_file:
        settings = load_env_file(args.env_file)
    else:
        sys.path.insert(0, str(ROOT / "scripts"))
        import local_stack

        up = local_stack.status()
        if not all(up.values()):
            raise SystemExit(f"Local disposable stack is not fully up: {up}. Run: python scripts/local_stack.py start")
        settings = local_stack.env_lines()
    for k, v in settings.items():
        if any(h in k.upper() for h in SECRET_HINTS) and not k.upper().endswith("_FILE"):
            raise SystemExit(f"Refused: {k} looks like a secret VALUE; pass a *_FILE path instead.")
    env.update(settings)
    env["NVB_DIGITAL_TEST_ENV"] = "disposable"
    if args.allow_host:
        env["NVB_DIGITAL_TEST_ALLOW_HOSTS"] = ",".join(args.allow_host)
    if args.core_dir.exists():
        env["CORE_BACKEND_DIR"] = str(args.core_dir)
    print("Targets (no credentials): " + ", ".join(
        f"{k}={v}" for k, v in sorted(settings.items()) if k.endswith(("_URL", "_BUCKET"))))
    cmd = [sys.executable, "-m", "pytest", "tests/integration", "-p", "no:cacheprovider", "-o", "addopts=",
           "--import-mode=importlib", "-q", *args.pytest_args]
    return subprocess.call(cmd, cwd=ROOT, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
