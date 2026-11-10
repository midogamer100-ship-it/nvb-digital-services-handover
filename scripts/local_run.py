"""Run the whole extension stack locally over real sockets (no containers) for manual demos and
the gateway/browser tests. DISPOSABLE data only: it uses the databases, vhost and bucket of
``scripts/local_stack.py`` and RESETS them on ``up``.

    python scripts/local_stack.py start
    python scripts/local_run.py up        # patched core + adapter + statements + loans + relays + nginx gateway
    python scripts/local_run.py worker statements   # one bounded worker run (like a Kubernetes Job)
    python scripts/local_run.py worker loans
    python scripts/local_run.py grant statements batch_operator <subject>
    python scripts/local_run.py down

Portal: http://127.0.0.1:18080  (demo passwords are generated into .local/run/demo-passwords.json)

Local-only relaxations, never used in the OpenShift manifests: plain http between local processes
(``*_ALLOW_INSECURE_HTTP`` / ``GATEWAY_ALLOW_INSECURE_UPSTREAMS``), non-Secure cookie, and the core's
``NVB_INTEGRATION_SETTLE_SECONDS`` left at its default.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import local_stack  # noqa: E402

RUN = ROOT / ".local" / "run"
HOST = "127.0.0.1"
PORTS = {"gateway": 18080, "core": 18100, "adapter": 18110, "statements": 18120, "loans": 18130}
NGINX_DIR = ROOT / ".local" / "nginx" / "nginx-1.26.2"
STRIP = ("NVB_", "ADAPTER_", "STATEMENTS_", "LOANS_", "GATEWAY_", "PG", "AWS_", "TEST_", "KUBERNETES_", "UVICORN_")
PY = sys.executable
PYTHONPATH = os.pathsep.join(str(ROOT / p) for p in ("libs/nvb_platform", "services/core-adapter",
                                                     "services/statements", "services/loans"))
DETACHED = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def base_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(STRIP)}
    env["PYTHONPATH"] = PYTHONPATH
    env["PYTHONUNBUFFERED"] = "1"
    return env


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def token(name: str) -> str:
    p = RUN / "tokens" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    return p.read_text(encoding="utf-8")


def wait_http(port: int, path: str, seconds: float = 60) -> None:
    import urllib.request

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://{HOST}:{port}{path}", timeout=2) as r:  # noqa: S310 - loopback
                if r.status < 500:
                    return
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    raise SystemExit(f"service on port {port} did not become ready (see .local/run/logs)")


def spawn(name: str, cmd: list[str], env: dict, cwd: Path | None = None) -> None:
    (RUN / "logs").mkdir(parents=True, exist_ok=True)
    log = open(RUN / "logs" / f"{name}.log", "ab")  # noqa: SIM115 - handed to the child
    proc = subprocess.Popen(cmd, env=env, cwd=str(cwd or ROOT), stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                            creationflags=DETACHED)
    pids = json.loads((RUN / "pids.json").read_text()) if (RUN / "pids.json").exists() else {}
    pids[name] = proc.pid
    (RUN / "pids.json").write_text(json.dumps(pids))


def service_env(prefix: str, db_index: int, adapter_client: str, queue_extra: dict) -> dict:
    s = local_stack.SECRETS
    db = local_stack.DATABASES[db_index]
    env = base_env()
    env.update({
        f"{prefix}_DATABASE_URL": f"postgresql+psycopg://{db}_app@{HOST}:{local_stack.PG_PORT}/{db}",
        f"{prefix}_DATABASE_PASSWORD_FILE": str(s / f"pg-{db}-app-password"),
        f"{prefix}_ADAPTER_BASE_URL": f"http://{HOST}:{PORTS['adapter']}", f"{prefix}_ALLOW_INSECURE_HTTP": "true",
        f"{prefix}_ADAPTER_TOKEN_FILE": str(RUN / "tokens" / adapter_client),
        f"{prefix}_BROKER_URL": f"amqp://{HOST}:{local_stack.AMQP_PORT}/{local_stack.VHOST}",
        f"{prefix}_BROKER_USERNAME_FILE": str(s / "rabbitmq-username"),
        f"{prefix}_BROKER_PASSWORD_FILE": str(s / "rabbitmq-password"),
        f"{prefix}_ALLOW_PLAINTEXT_DEPENDENCIES": "true", f"{prefix}_IDLE_EXIT_SECONDS": "3",
        f"{prefix}_RETRY_BASE_SECONDS": "5", f"{prefix}_LOG_LEVEL": "INFO", **queue_extra})
    return env


def statements_env(client: str) -> dict:
    s = local_stack.SECRETS
    return service_env("STATEMENTS", 1, client, {
        "STATEMENTS_LEASE_SECONDS": "15",
        "STATEMENTS_QUEUE_NAME": "nvb.local.statements.generate", "STATEMENTS_BATCH_QUEUE_NAME": "nvb.local.statements.batch",
        "STATEMENTS_S3_ENDPOINT_URL": f"http://{HOST}:{local_stack.S3_PORT}", "STATEMENTS_S3_BUCKET": local_stack.BUCKETS[0],
        "STATEMENTS_S3_ACCESS_KEY_FILE": str(s / "s3-access-key"), "STATEMENTS_S3_SECRET_KEY_FILE": str(s / "s3-secret-key")})


def loans_env(client: str) -> dict:
    return service_env("LOANS", 2, client, {"LOANS_QUEUE_NAME": "nvb.local.loans.summarize"})


def migrate(service: str, db_index: int) -> None:
    db = local_stack.DATABASES[db_index]
    env = base_env()
    up = service.upper()
    env.update({f"{up}_MIGRATION_DATABASE_URL": f"postgresql+psycopg://{db}_owner@{HOST}:{local_stack.PG_PORT}/{db}",
                f"{up}_MIGRATION_PASSWORD_FILE": str(local_stack.SECRETS / f"pg-{db}-password"),
                f"{up}_APP_ROLE": f"{db}_app"})
    subprocess.run([PY, "-m", "alembic", "-c", str(ROOT / "migrations" / service / "alembic.ini"), "upgrade", "head"],
                   env=env, cwd=str(ROOT), check=True, capture_output=True)


def reset_db(db: str) -> None:
    """Drop and recreate the public schema of a database of THE LOCAL disposable stack only."""
    marker = local_stack.psql("SELECT shobj_description(oid,'pg_database') FROM pg_database "
                              f"WHERE datname='{db}'")
    if marker != "nvb-disposable-test" or not db.startswith("nvb_disposable_test_"):
        raise SystemExit(f"Refused: {db} is not a marked disposable database.")
    local_stack.psql(f"DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public AUTHORIZATION {db}_owner; "
                     "GRANT USAGE ON SCHEMA public TO PUBLIC", db)


def up(core_dir: Path) -> None:
    if not all(local_stack.status().values()):
        raise SystemExit("Start the disposable dependencies first: python scripts/local_stack.py start")
    down()
    shutil.rmtree(RUN, ignore_errors=True)
    RUN.mkdir(parents=True)
    for db in local_stack.DATABASES:
        reset_db(db)
    migrate("statements", 1)
    migrate("loans", 2)

    # --- credentials (generated per run; only digests go into the *clients* files) ----------------
    core_scopes = ["identity:introspect", "accounts:read", "statements:extract", "summaries:read", "accounts:discover"]
    (RUN / "core-clients.json").write_text(json.dumps({"clients": [
        {"client_id": "nvb-core-adapter", "token_sha256": [digest(token("core-adapter"))], "scopes": core_scopes}]}))
    adapter_clients = {"statements-api": ["identity:resolve", "accounts:read", "discovery:read"],
                       "statements-worker": ["statements:read"],
                       "loans-api": ["identity:resolve", "accounts:read"],
                       "loans-worker": ["summaries:read"]}
    (RUN / "adapter-clients.json").write_text(json.dumps({"clients": [
        {"client_id": n, "token_sha256": [digest(token(n))], "scopes": sc} for n, sc in adapter_clients.items()]}))
    passwords = {g: secrets.token_urlsafe(12) for g in ("customer", "loadtest", "demo", "admin")}
    (RUN / "demo-passwords.json").write_text(json.dumps(passwords, indent=2))

    # --- patched core on its own disposable database, synthetic seed ------------------------------
    core_db = local_stack.DATABASES[0]
    core_pw = (local_stack.SECRETS / f"pg-{core_db}-password").read_text().strip()
    static = RUN / "core-static"
    static.mkdir()
    (static / "index.html").write_text("<html><body>CORE-SPA-FALLBACK-MARKER</body></html>")
    core_env = base_env()
    core_env.update({
        "PYTHONPATH": str(core_dir),
        "NVB_DATABASE_URL": f"postgresql+psycopg://{core_db}_owner:{core_pw}@{HOST}:{local_stack.PG_PORT}/{core_db}",
        "NVB_COOKIE_SECURE": "false", "NVB_INTEGRATION_ENABLED": "true",
        "NVB_INTEGRATION_CLIENTS_FILE": str(RUN / "core-clients.json"), "NVB_STATIC_DIR": str(static),
        "NVB_ENVIRONMENT_LABEL": "Local disposable", "NVB_ARGON2_TIME_COST": "1", "NVB_ARGON2_MEMORY_COST_KIB": "8192",
        "NVB_LOGIN_MAX_FAILURES_PER_IP": "1000"})
    seed = ("import json,sys;from app.config import Settings;from app.db import Base;"
            "from app.main import create_app;from app.services.seed import seed_database;"
            "a=create_app(Settings());Base.metadata.create_all(a.state.engine);d=a.state.sessionmaker();"
            "seed_database(d,a.state.passwords,json.load(open(sys.argv[1])),customer_count=4,loadtest_count=2,"
            "demo_count=1);d.close()")
    subprocess.run([PY, "-c", seed, str(RUN / "demo-passwords.json")], env=core_env, cwd=str(core_dir), check=True,
                   capture_output=True)
    uv = [PY, "-m", "uvicorn", "--factory", "--host", HOST, "--no-server-header", "--log-level", "warning"]
    spawn("core", [*uv, "app.main:app_factory", "--port", str(PORTS["core"])], core_env, core_dir)

    adapter_env = base_env()
    adapter_env.update({"ADAPTER_CORE_BASE_URL": f"http://{HOST}:{PORTS['core']}", "ADAPTER_ALLOW_INSECURE_HTTP": "true",
                        "ADAPTER_CORE_TOKEN_FILE": str(RUN / "tokens" / "core-adapter"),
                        "ADAPTER_CLIENTS_FILE": str(RUN / "adapter-clients.json")})
    spawn("adapter", [*uv, "core_adapter.app:app_factory", "--port", str(PORTS["adapter"])], adapter_env)
    spawn("statements-api", [*uv, "nvb_statements.api:app_factory", "--port", str(PORTS["statements"])],
          statements_env("statements-api"))
    spawn("loans-api", [*uv, "nvb_loans.api:app_factory", "--port", str(PORTS["loans"])], loans_env("loans-api"))
    spawn("statements-relay", [PY, "-m", "nvb_statements.relay"], statements_env("statements-api"))
    spawn("loans-relay", [PY, "-m", "nvb_loans.relay"], loans_env("loans-api"))

    # --- gateway (real nginx, same template as the container image) -------------------------------
    gw_env = base_env()
    gw_run = RUN / "nginx"
    (gw_run / "logs").mkdir(parents=True)
    gw_env.update({
        "GATEWAY_LISTEN": f"{HOST}:{PORTS['gateway']}", "GATEWAY_PUBLIC_ORIGIN": f"http://{HOST}:{PORTS['gateway']}",
        "GATEWAY_ALLOW_INSECURE_UPSTREAMS": "true", "GATEWAY_CORE_UPSTREAM": f"http://{HOST}:{PORTS['core']}",
        "GATEWAY_STATEMENTS_UPSTREAM": f"http://{HOST}:{PORTS['statements']}",
        "GATEWAY_LOANS_UPSTREAM": f"http://{HOST}:{PORTS['loans']}", "GATEWAY_PORTAL_ROOT": str(ROOT / "portal"),
        "GATEWAY_RUN_DIR": str(gw_run), "GATEWAY_MIME_TYPES": str(NGINX_DIR / "conf" / "mime.types"),
        "GATEWAY_ERROR_LOG": str(gw_run / "logs" / "error.log"), "GATEWAY_ACCESS_LOG": str(gw_run / "logs" / "access.log"),
        "GATEWAY_WORKER_PROCESSES": "1", "GATEWAY_LOGIN_RATE": "600r/m", "GATEWAY_LOGIN_BURST": "200"})
    subprocess.run([PY, str(ROOT / "gateway" / "render_config.py"), "--output", str(gw_run / "nginx.conf")],
                   env=gw_env, check=True)
    nginx = [str(NGINX_DIR / "nginx.exe"), "-p", str(gw_run), "-c", str(gw_run / "nginx.conf")]
    subprocess.run([*nginx, "-t"], check=True, capture_output=True, cwd=str(NGINX_DIR))
    spawn("gateway", nginx, gw_env, NGINX_DIR)

    wait_http(PORTS["core"], "/health/ready")
    wait_http(PORTS["adapter"], "/health/ready")
    wait_http(PORTS["statements"], "/health/ready")
    wait_http(PORTS["loans"], "/health/ready")
    wait_http(PORTS["gateway"], "/gateway/health")
    print(json.dumps({"portal": f"http://{HOST}:{PORTS['gateway']}", "ports": PORTS,
                      "demo_passwords_file": str(RUN / "demo-passwords.json"),
                      "users": "customer001..customer004, opsadmin (group admin), demoload01"}))


def down() -> None:
    pid_file = RUN / "pids.json"
    if pid_file.exists():
        for name, pid in json.loads(pid_file.read_text()).items():
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True) if os.name == "nt" \
                else os.kill(pid, 15)
        pid_file.unlink()
    for port in PORTS.values():
        for _ in range(20):
            with socket.socket() as s:
                s.settimeout(0.3)
                if s.connect_ex((HOST, port)) != 0:
                    break
            time.sleep(0.25)


def worker(service: str, extra: list[str]) -> int:
    env = statements_env("statements-worker") if service == "statements" else loans_env("loans-worker")
    return subprocess.call([PY, "-m", f"nvb_{service}.worker", "--drain", *extra], env=env, cwd=str(ROOT))


def admin(service: str, args: list[str]) -> int:
    env = statements_env("statements-api") if service == "statements" else loans_env("loans-api")
    return subprocess.call([PY, "-m", f"nvb_{service}.admin", *args], env=env, cwd=str(ROOT))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["up", "down", "worker", "grant", "batch"])
    p.add_argument("args", nargs="*")
    p.add_argument("--core-dir", type=Path, default=ROOT.parent / "NVB-Monolith-integration" / "backend")
    a, extra = p.parse_known_args()
    if a.command == "up":
        up(a.core_dir)
    elif a.command == "down":
        down()
    elif a.command == "worker":
        return worker(a.args[0], a.args[1:] + extra)
    elif a.command == "grant":
        service, role, subject = a.args
        return admin(service, ["grant", "--subject", subject, "--role", role, "--actor", "ops:local-run",
                               "--reason", "local demo"])
    elif a.command == "batch":   # the scheduled trigger command, run by hand
        env = statements_env("statements-api")
        return subprocess.call([PY, "-m", "nvb_statements.batch", "create", "--period", a.args[0] if a.args
                                else "previous-month", "--trigger", "schedule"], env=env, cwd=str(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
