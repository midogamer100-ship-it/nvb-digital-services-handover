"""Verify every Containerfile WITHOUT a container engine (this workstation has none).

For each image it

1. parses the Containerfile and checks that every COPY source exists relative to the declared
   build context (the repository root) and is not excluded by .containerignore;
2. assembles the image's /opt tree in a temporary directory exactly as the COPY lines would;
3. runs the image's real commands against that tree with a clean environment and PYTHONPATH set
   as in the image: module imports, CLI ``--help`` and - when a disposable PostgreSQL is supplied
   (``--with-postgres``, uses the local stack) - the MIGRATION command from the assembled tree.

This proves "the migration files are in the image layout and the command works from there".
It is NOT an image build: base image contents, pip resolution inside UBI and file permissions are
only proven by scripts/build-images.sh on a machine with podman.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGES = {
    "nvb-core-adapter": ("services/core-adapter/Containerfile", ["core_adapter.app"], []),
    "nvb-statements": ("services/statements/Containerfile",
                       ["nvb_statements.api", "nvb_statements.relay", "nvb_statements.worker", "nvb_statements.batch",
                        "nvb_statements.admin"],
                       [["-m", "nvb_statements.worker", "--help"], ["-m", "nvb_statements.batch", "--help"],
                        ["-m", "nvb_statements.admin", "--help"]]),
    "nvb-loans": ("services/loans/Containerfile",
                  ["nvb_loans.api", "nvb_loans.relay", "nvb_loans.worker", "nvb_loans.admin"],
                  [["-m", "nvb_loans.worker", "--help"], ["-m", "nvb_loans.relay", "--help"],
                   ["-m", "nvb_loans.admin", "--help"]]),
    "nvb-gateway": ("gateway/Containerfile", [], []),
}


def ignored(rel: str, patterns: list[str]) -> bool:
    parts = rel.split("/")
    for pat in patterns:
        bare = pat[3:] if pat.startswith("**/") else pat
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, bare) or rel.startswith(bare.rstrip("/") + "/") \
                or (pat.startswith("**/") and any(fnmatch.fnmatch(p, bare) for p in parts)):
            return True
    return False


def assemble(containerfile: Path, dest: Path, patterns: list[str]) -> list[str]:
    problems = []
    for line in containerfile.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^COPY\s+(?!--from)(.+)$", line.strip())
        if not m:
            continue
        *sources, target = m.group(1).split()
        for src in sources:
            path = ROOT / src
            if not path.exists():
                problems.append(f"COPY source missing: {src}")
                continue
            if ignored(src, patterns):
                problems.append(f"COPY source excluded by .containerignore: {src}")
                continue
            if target == "requirements.txt":
                continue
            out = dest / target.lstrip("/")
            if path.is_dir():
                shutil.copytree(path, out, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests"))
            else:
                final = out / path.name if target.endswith("/") else out
                final.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, final)
    return problems


def clean_env(app: Path) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("NVB_", "ADAPTER_", "STATEMENTS_", "LOANS_", "GATEWAY_", "PG", "PYTHONPATH"))}
    env["PYTHONPATH"] = str(app)
    return env


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--with-postgres", action="store_true", help="also run the migration commands (local stack)")
    args = p.parse_args()
    patterns = [l.strip() for l in (ROOT / ".containerignore").read_text().splitlines()
                if l.strip() and not l.startswith("#")]
    failed = False
    for image, (cf, modules, commands) in IMAGES.items():
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            problems = assemble(ROOT / cf, dest, patterns)
            app = dest / "opt" / "app"
            env = clean_env(app)
            for mod in modules:
                r = subprocess.run([sys.executable, "-c", f"import {mod}"], env=env, cwd=tmp, capture_output=True, text=True)
                if r.returncode:
                    problems.append(f"import {mod} failed: {r.stderr.strip().splitlines()[-1]}")
            for cmd in commands:
                r = subprocess.run([sys.executable, *cmd], env=env, cwd=tmp, capture_output=True, text=True)
                if r.returncode:
                    problems.append(f"{' '.join(cmd)} failed")
            for service in ("statements", "loans"):
                if image != f"nvb-{service}":
                    continue
                ini = app / "migrations" / service / "alembic.ini"
                if not ini.exists() or not list((ini.parent / "versions").glob("0*.py")):
                    problems.append("migration files are NOT in the image layout")
                elif args.with_postgres:
                    sys.path.insert(0, str(ROOT / "scripts"))
                    import local_run
                    import local_stack

                    idx = 1 if service == "statements" else 2
                    db = local_stack.DATABASES[idx]
                    local_run.reset_db(db)
                    up = service.upper()
                    menv = {**env, f"{up}_MIGRATION_DATABASE_URL":
                            f"postgresql+psycopg://{db}_owner@127.0.0.1:{local_stack.PG_PORT}/{db}",
                            f"{up}_MIGRATION_PASSWORD_FILE": str(local_stack.SECRETS / f"pg-{db}-password"),
                            f"{up}_APP_ROLE": f"{db}_app"}
                    r = subprocess.run([sys.executable, "-m", "alembic", "-c", str(ini), "upgrade", "head"], env=menv,
                                       cwd=tmp, capture_output=True, text=True)
                    head = local_stack.psql(f"SELECT version_num FROM alembic_version_{service}", db) if not r.returncode else ""
                    if r.returncode or not head:
                        problems.append(f"migration command failed from the image layout: {r.stderr[-300:]}")
                    else:
                        print(f"  {image}: migration command OK from the assembled layout -> {head}")
            if image == "nvb-gateway":
                for needed in ("opt/gateway/nginx.conf.template", "opt/gateway/render_config.py",
                               "opt/gateway/entrypoint.sh", "opt/portal/index.html", "opt/portal/app.js"):
                    if not (dest / needed).exists():
                        problems.append(f"missing in layout: {needed}")
            print(f"{image}: {'OK' if not problems else 'FAILED'}")
            for pr in problems:
                failed = True
                print(f"  - {pr}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
