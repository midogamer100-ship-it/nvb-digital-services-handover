"""Disposable local dependency stack WITHOUT a container runtime (Windows workstation).

Starts real PostgreSQL 16, RabbitMQ 3.13 (on Erlang/OTP 26) and an S3-compatible object
store (SeaweedFS) from portable archives extracted under ``.local/`` (git-ignored).
Everything binds to 127.0.0.1 only, uses non-default ports and freshly generated random
credentials written to ``.local/secrets/`` (never printed, never committed).

This stack is DISPOSABLE: ``destroy`` deletes all of its data. It never touches any other
PostgreSQL/RabbitMQ/S3 instance: every command addresses 127.0.0.1 on the ports below.

    python scripts/local_stack.py fetch      # download + extract portable archives (once)
    python scripts/local_stack.py start      # init (first time) and start all three
    python scripts/local_stack.py status
    python scripts/local_stack.py env        # print NON-secret env (URLs + *_FILE paths)
    python scripts/local_stack.py stop
    python scripts/local_stack.py destroy    # stop and delete all local data + credentials

Teams with podman/docker can use deploy/local/compose.yaml instead (same ports/names).
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local"
SECRETS = LOCAL / "secrets"
HOST = "127.0.0.1"
PG_PORT, AMQP_PORT, RMQ_DIST_PORT, EPMD_PORT = 55432, 55672, 55673, 54369
# SeaweedFS also listens on <port>+10000 (gRPC), so these stay below 55536.
S3_PORT, WEED_MASTER, WEED_VOLUME, WEED_FILER = 18433, 19433, 18180, 18988
VHOST = "nvb-test"
DATABASES = ("nvb_disposable_test_core", "nvb_disposable_test_statements", "nvb_disposable_test_loans")
BUCKETS = ("nvb-test-statements",)

ARCHIVES = {  # name -> (url, sha256 recorded when this script was written)
    "pg": ("https://get.enterprisedb.com/postgresql/postgresql-16.6-1-windows-x64-binaries.zip",
           "6a1bfb6435b13d9563ae481445c70ac2a19846bd8a430b12903b408eec300f9b"),
    "otp": ("https://github.com/erlang/otp/releases/download/OTP-26.2.5.6/otp_win64_26.2.5.6.zip",
            "bd2215eb8017dcc3fb7279d0e91b9202bf19806a7f875bbe576ea723db99a032"),
    "rabbit": ("https://github.com/rabbitmq/rabbitmq-server/releases/download/v3.13.7/"
               "rabbitmq-server-windows-3.13.7.zip",
               "eb1967ad89834e8de50e36d48a88b2ab46fdc4a6062db36b0682e3259084a102"),
    "weed": ("https://github.com/seaweedfs/seaweedfs/releases/download/3.97/windows_amd64.zip",
             "6d37d920251a6cbd1c3e573c653bcbe3bd1e36031c2c4990ec4ea38e53ad882e"),
    "nginx": ("https://nginx.org/download/nginx-1.26.2.zip",
              "942638cc31c836fe429fde22e4c46e497f89d3c2bbce46cd2c8800854fd71409"),
}

PG_BIN = LOCAL / "pg" / "pgsql" / "bin"
RMQ_SBIN = LOCAL / "rabbit" / "rabbitmq_server-3.13.7" / "sbin"
EPMD = LOCAL / "otp" / "erts-14.2.5.5" / "bin" / "epmd.exe"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACHED = NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def secret(name: str, nbytes: int = 24) -> str:
    """Return the secret stored in .local/secrets/<name>, creating it on first use."""
    SECRETS.mkdir(parents=True, exist_ok=True)
    p = SECRETS / name
    if not p.exists():
        p.write_text(secrets.token_urlsafe(nbytes), encoding="utf-8")
    return p.read_text(encoding="utf-8").strip()


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((HOST, port)) == 0


def wait_port(port: int, seconds: float, what: str) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if port_open(port):
            return
        time.sleep(0.5)
    raise SystemExit(f"{what} did not start listening on {HOST}:{port}")


def run(cmd: list[str], env: dict | None = None, check: bool = True, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([str(c) for c in cmd], env=env, check=check, capture_output=True, text=True,
                          creationflags=NO_WINDOW, **kw)


def clean_env() -> dict:
    """Base environment without inherited application/DB/cloud settings."""
    drop = ("NVB_", "ADAPTER_", "STATEMENTS_", "LOANS_", "PG", "AWS_", "RABBITMQ_", "ERL")
    return {k: v for k, v in os.environ.items() if not k.upper().startswith(drop)}


# --- fetch ------------------------------------------------------------------------------


def fetch() -> None:
    import hashlib

    (LOCAL / "dl").mkdir(parents=True, exist_ok=True)
    for name, (url, digest) in ARCHIVES.items():
        target = LOCAL / name
        if target.exists() and any(target.iterdir()):
            continue
        archive = LOCAL / "dl" / f"{name}.zip"
        if not archive.exists():
            print(f"downloading {name} ...")
            urllib.request.urlretrieve(url, archive)  # noqa: S310 - fixed https URLs above
        actual = hashlib.sha256(archive.read_bytes()).hexdigest()
        if actual != digest:
            raise SystemExit(f"{name}: checksum mismatch (got {actual}); refusing to extract")
        target.mkdir(exist_ok=True)
        with zipfile.ZipFile(archive) as z:
            z.extractall(target)
    print("archives ready under .local/")


# --- PostgreSQL -------------------------------------------------------------------------


def pg_env() -> dict:
    env = clean_env()
    env.update(PGHOST=HOST, PGPORT=str(PG_PORT), PGUSER="nvb_local_admin", PGPASSWORD=secret("pg-admin-password"),
               PGCONNECT_TIMEOUT="5")
    return env


def psql(sql: str, db: str = "postgres") -> str:
    return run([PG_BIN / "psql.exe", "-v", "ON_ERROR_STOP=1", "-X", "-q", "-At", "-d", db, "-c", sql],
               env=pg_env()).stdout.strip()


def start_pg() -> None:
    data = LOCAL / "pg-data"
    if not (data / "PG_VERSION").exists():
        pwfile = SECRETS / "pg-admin-password"
        secret("pg-admin-password")
        run([PG_BIN / "initdb.exe", "-D", data, "-U", "nvb_local_admin", f"--pwfile={pwfile}",
             "-A", "scram-sha-256", "-E", "UTF8", "--locale=C"], env=clean_env())
    if not port_open(PG_PORT):
        opts = f"-p {PG_PORT} -c listen_addresses={HOST} -c max_connections=120 -c fsync=off"
        subprocess.Popen([str(PG_BIN / "pg_ctl.exe"), "-D", str(data), "-l", str(LOCAL / "pg.log"), "-o", opts,
                          "-w", "start"], env=clean_env(), creationflags=DETACHED,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        wait_port(PG_PORT, 60, "PostgreSQL")
        time.sleep(1)
    for db in DATABASES:
        role = db + "_owner"
        pw = secret(f"pg-{db}-password")
        if not psql(f"SELECT 1 FROM pg_roles WHERE rolname='{role}'"):
            psql(f"CREATE ROLE {role} LOGIN PASSWORD '{pw}' NOSUPERUSER NOCREATEDB NOCREATEROLE")
        if not psql(f"SELECT 1 FROM pg_database WHERE datname='{db}'"):
            psql(f"CREATE DATABASE {db} OWNER {role}")
            psql(f"REVOKE ALL ON DATABASE {db} FROM PUBLIC")
            # Positive marker checked by scripts/safe_core_tests.py and tests/integration before any DROP.
            psql(f"COMMENT ON DATABASE {db} IS 'nvb-disposable-test'")
        if db != DATABASES[0]:
            # Least-privilege runtime role (the services use it; migrations run as the owner).
            app, app_pw = db + "_app", secret(f"pg-{db}-app-password")
            if not psql(f"SELECT 1 FROM pg_roles WHERE rolname='{app}'"):
                psql(f"CREATE ROLE {app} LOGIN PASSWORD '{app_pw}' NOSUPERUSER NOCREATEDB NOCREATEROLE")
            psql(f"GRANT CONNECT ON DATABASE {db} TO {app}")


def stop_pg() -> None:
    data = LOCAL / "pg-data"
    if (data / "postmaster.pid").exists():
        run([PG_BIN / "pg_ctl.exe", "-D", data, "-m", "fast", "-w", "stop"], env=clean_env(), check=False)


# --- RabbitMQ ---------------------------------------------------------------------------


def rmq_env() -> dict:
    env = clean_env()
    home = LOCAL / "rabbit-home"  # keeps the Erlang cookie out of the real user profile
    home.mkdir(parents=True, exist_ok=True)
    env.update(
        ERLANG_HOME=str(LOCAL / "otp"), RABBITMQ_BASE=str(LOCAL / "rabbit-data"),
        RABBITMQ_NODENAME="nvbtest@localhost", RABBITMQ_NODE_IP_ADDRESS=HOST, RABBITMQ_NODE_PORT=str(AMQP_PORT),
        RABBITMQ_DIST_PORT=str(RMQ_DIST_PORT), ERL_EPMD_ADDRESS=HOST, ERL_EPMD_PORT=str(EPMD_PORT),
        HOMEDRIVE=home.drive, HOMEPATH=str(home)[len(home.drive):], USERPROFILE=str(home),
        RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS="-kernel inet_dist_use_interface {127,0,0,1}",
        ERL_CRASH_DUMP=str(LOCAL / "erl_crash.dump"),
    )
    return env


def rabbitmqctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    # rabbitmqctl is a .bat on Windows: every argument is double-quoted so that cmd.exe does not
    # interpret regex characters such as ^ and | (unquoted, "^nvb" silently loses its anchor).
    quoted = " ".join(f'"{a}"' for a in (str(RMQ_SBIN / "rabbitmqctl.bat"), *args))
    out = subprocess.run(f'cmd /s /c "{quoted}"', env=rmq_env(), capture_output=True, text=True,
                         creationflags=NO_WINDOW)
    if check and out.returncode != 0:
        raise subprocess.CalledProcessError(out.returncode, ["rabbitmqctl", *args[:1]], out.stdout, out.stderr)
    return out


def start_rabbit() -> None:
    env = rmq_env()
    if not port_open(AMQP_PORT):
        if not port_open(EPMD_PORT):  # start the Erlang port mapper explicitly, loopback only
            subprocess.Popen([str(EPMD), "-address", HOST, "-port", str(EPMD_PORT)], env=env,
                             creationflags=DETACHED, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL, cwd=str(LOCAL))
            wait_port(EPMD_PORT, 20, "epmd")
        log = open(LOCAL / "rabbit.log", "ab")  # noqa: SIM115 - handed to the detached child
        subprocess.Popen(["cmd", "/c", str(RMQ_SBIN / "rabbitmq-server.bat")], env=env, creationflags=DETACHED,
                         stdout=log, stderr=log, stdin=subprocess.DEVNULL, cwd=str(LOCAL))
        wait_port(AMQP_PORT, 120, "RabbitMQ")
    rabbitmqctl("await_startup")
    user, pw = "nvb_test", secret("rabbitmq-password")
    (SECRETS / "rabbitmq-username").write_text(user, encoding="utf-8")
    vhosts = rabbitmqctl("list_vhosts", "--quiet").stdout.split()
    if VHOST not in vhosts:
        rabbitmqctl("add_vhost", VHOST, "--default-queue-type", "quorum")
    users = rabbitmqctl("list_users", "--quiet").stdout
    if user not in users:
        run(["cmd", "/c", str(RMQ_SBIN / "rabbitmqctl.bat"), "add_user", user], env=env, input=pw + "\n")
    # configure/read: only nvb.* resources. write: nvb.* plus the default exchange, which the
    # services publish through (routing key = queue name). Nothing else in the vhost is reachable.
    rabbitmqctl("set_permissions", "-p", VHOST, user, "^nvb\\..*", "^(nvb\\..*|amq\\.default)$", "^nvb\\..*")
    if "guest" in users:
        rabbitmqctl("delete_user", "guest", check=False)


def stop_rabbit() -> None:
    if port_open(AMQP_PORT):
        rabbitmqctl("stop", check=False)
    run([EPMD, "-port", str(EPMD_PORT), "-kill"], env=rmq_env(), check=False)


# --- S3 (SeaweedFS) ---------------------------------------------------------------------


def start_s3() -> None:
    data = LOCAL / "weed-data"
    data.mkdir(parents=True, exist_ok=True)
    access, key = secret("s3-access-key", 15), secret("s3-secret-key", 30)
    cfg = LOCAL / "weed-s3.json"
    cfg.write_text(json.dumps({"identities": [{
        "name": "nvb-test", "credentials": [{"accessKey": access, "secretKey": key}],
        "actions": ["Admin", "Read", "Write", "List", "Tagging"]}]}), encoding="utf-8")
    if not port_open(S3_PORT):
        log = open(LOCAL / "weed.log", "ab")  # noqa: SIM115
        subprocess.Popen(
            [str(LOCAL / "weed" / "weed.exe"), "server", f"-dir={data}", f"-ip={HOST}", f"-ip.bind={HOST}",
             f"-master.port={WEED_MASTER}", f"-volume.port={WEED_VOLUME}", "-volume.max=20", "-filer",
             f"-filer.port={WEED_FILER}", "-s3", f"-s3.port={S3_PORT}", f"-s3.config={cfg}"],
            env=clean_env(), creationflags=DETACHED, stdout=log, stderr=log, stdin=subprocess.DEVNULL, cwd=str(data))
        wait_port(S3_PORT, 90, "SeaweedFS S3")
        time.sleep(3)
    import boto3
    from botocore.config import Config

    s3 = boto3.client("s3", endpoint_url=f"http://{HOST}:{S3_PORT}", region_name="us-east-1",
                      aws_access_key_id=access, aws_secret_access_key=key,
                      config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 5}))
    existing = {b["Name"] for b in s3.list_buckets().get("Buckets", [])}
    for b in BUCKETS:
        if b not in existing:
            s3.create_bucket(Bucket=b)


def stop_s3() -> None:
    run(["taskkill", "/F", "/IM", "weed.exe"], check=False)


# --- commands ---------------------------------------------------------------------------


def env_lines() -> dict[str, str]:
    """Non-secret settings only: URLs without credentials plus paths of secret FILES."""
    s = SECRETS
    return {
        "NVB_DIGITAL_TEST_ENV": "disposable",
        "NVB_LOCAL_PG_HOST": HOST, "NVB_LOCAL_PG_PORT": str(PG_PORT),
        "TEST_CORE_DATABASE_URL": f"postgresql+psycopg://{DATABASES[0]}_owner@{HOST}:{PG_PORT}/{DATABASES[0]}",
        "TEST_CORE_DATABASE_PASSWORD_FILE": str(s / f"pg-{DATABASES[0]}-password"),
        "TEST_STATEMENTS_DATABASE_URL": f"postgresql+psycopg://{DATABASES[1]}_owner@{HOST}:{PG_PORT}/{DATABASES[1]}",
        "TEST_STATEMENTS_DATABASE_PASSWORD_FILE": str(s / f"pg-{DATABASES[1]}-password"),
        "TEST_LOANS_DATABASE_URL": f"postgresql+psycopg://{DATABASES[2]}_owner@{HOST}:{PG_PORT}/{DATABASES[2]}",
        "TEST_LOANS_DATABASE_PASSWORD_FILE": str(s / f"pg-{DATABASES[2]}-password"),
        "TEST_STATEMENTS_APP_ROLE": f"{DATABASES[1]}_app",
        "TEST_STATEMENTS_APP_PASSWORD_FILE": str(s / f"pg-{DATABASES[1]}-app-password"),
        "TEST_LOANS_APP_ROLE": f"{DATABASES[2]}_app",
        "TEST_LOANS_APP_PASSWORD_FILE": str(s / f"pg-{DATABASES[2]}-app-password"),
        "TEST_BROKER_URL": f"amqp://{HOST}:{AMQP_PORT}/{VHOST}",
        "TEST_BROKER_USERNAME_FILE": str(s / "rabbitmq-username"),
        "TEST_BROKER_PASSWORD_FILE": str(s / "rabbitmq-password"),
        "TEST_S3_ENDPOINT_URL": f"http://{HOST}:{S3_PORT}",
        "TEST_S3_BUCKET": BUCKETS[0],
        "TEST_S3_ACCESS_KEY_FILE": str(s / "s3-access-key"),
        "TEST_S3_SECRET_KEY_FILE": str(s / "s3-secret-key"),
    }


def status() -> dict[str, bool]:
    return {"postgresql": port_open(PG_PORT), "rabbitmq": port_open(AMQP_PORT), "s3": port_open(S3_PORT)}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["fetch", "start", "stop", "status", "env", "destroy"])
    p.add_argument("--only", choices=["pg", "rabbit", "s3"], action="append")
    args = p.parse_args()
    only = set(args.only or ["pg", "rabbit", "s3"])
    if sys.platform != "win32" and args.command in ("fetch", "start"):
        raise SystemExit("Portable archives are Windows builds; on Linux/macOS use deploy/local/compose.yaml.")
    if args.command == "fetch":
        fetch()
    elif args.command == "start":
        try:
            if "pg" in only:
                start_pg()
            if "rabbit" in only:
                start_rabbit()
            if "s3" in only:
                start_s3()
        except subprocess.CalledProcessError as exc:
            # stderr of local tools may be shown; it never contains the generated secrets.
            raise SystemExit(f"command failed: {Path(exc.cmd[0]).name} ...\n{(exc.stderr or '')[-800:]}") from None
        print(json.dumps(status()))
    elif args.command == "stop":
        if "s3" in only:
            stop_s3()
        if "rabbit" in only:
            stop_rabbit()
        if "pg" in only:
            stop_pg()
        print(json.dumps(status()))
    elif args.command == "status":
        print(json.dumps(status()))
    elif args.command == "env":
        for k, v in env_lines().items():
            print(f"{k}={v}")
    elif args.command == "destroy":
        stop_s3(), stop_rabbit(), stop_pg()
        time.sleep(2)
        for d in ("pg-data", "rabbit-data", "rabbit-home", "weed-data", "secrets"):
            shutil.rmtree(LOCAL / d, ignore_errors=True)
        print("local disposable data and credentials removed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
