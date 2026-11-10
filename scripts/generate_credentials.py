"""Generate every credential of one environment into a LOCAL, git-ignored directory and write the
`oc create secret` commands that load them (from files - values never appear on a command line).

    python scripts/generate_credentials.py --env test --out secrets/test
    python scripts/generate_credentials.py --env poc  --out secrets/poc --rabbitmq-host rabbit.example --rabbitmq-tls

What it creates (see deploy/openshift/secrets/README.md for the Secret <-> file mapping):

* service tokens (random, 256 bit) as RAW token files for the CALLER, and the matching
  ``clients.json`` files with SHA-256 DIGESTS + scopes for the CALLEE (core, core-adapter).
  The raw token of a client is never stored next to its digest list in the cluster.
* database, broker and object-storage passwords/keys;
* for ``--env test`` only: the disposable dependencies' own configuration (PostgreSQL admin
  password, RabbitMQ definitions with password HASHES, object-store identities, test-core seed
  passwords).

Nothing is printed except file names. The output directory must be outside version control
(``secrets/`` is in .gitignore). Rotation: see docs/CONFIGURATION.md ("Rotation").
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
from pathlib import Path

CORE_SCOPES = ["identity:introspect", "accounts:read", "statements:extract", "summaries:read", "accounts:discover"]
ADAPTER_CLIENTS = {
    "statements-api": ["identity:resolve", "accounts:read", "discovery:read"],
    "statements-worker": ["statements:read"],
    "statements-batch": ["discovery:read"],
    "loans-api": ["identity:resolve", "accounts:read"],
    "loans-worker": ["summaries:read"],
}


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def rabbit_hash(password: str) -> str:
    """RabbitMQ rabbit_password_hashing_sha256: base64(salt + sha256(salt + password))."""
    salt = os.urandom(4)
    return base64.b64encode(salt + hashlib.sha256(salt + password.encode()).digest()).decode()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env", choices=["test", "poc"], required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--namespace")
    p.add_argument("--rabbitmq-host", help="broker host[:port] for the KEDA monitoring URL")
    p.add_argument("--rabbitmq-tls", action="store_true")
    args = p.parse_args()
    ns = args.namespace or ("nvb-digital-test" if args.env == "test" else "nvb-digital")
    out: Path = args.out
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"Refused: {out} is not empty (rotation = generate into a new directory, see the docs).")
    out.mkdir(parents=True, exist_ok=True)
    cmds = ["#!/usr/bin/env bash", "# Loads the generated credentials as Secrets. Review, then run with a logged-in `oc`.",
            "# Values are read from files; nothing secret is on the command line.", "set -euo pipefail",
            f'NS="${{NS:-{ns}}}"', 'cd "$(dirname "$0")"', ""]

    def write(rel: str, value: str) -> str:
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8", newline="\n")
        return rel

    def secret(name: str, files: dict[str, str]) -> None:
        """files: key -> relative file path."""
        parts = " ".join(f"--from-file={k}={v}" for k, v in files.items())
        cmds.append(f'oc -n "$NS" create secret generic {name} {parts}')

    def rnd(nbytes: int = 32) -> str:
        return secrets.token_urlsafe(nbytes)

    # --- service-to-service credentials -------------------------------------------------------
    core_token = rnd()
    secret("core-adapter-core-token", {"token": write("tokens/core-adapter.token", core_token)})
    core_clients = write("core/integration-clients.json", json.dumps({"clients": [
        {"client_id": "nvb-core-adapter", "token_sha256": [digest(core_token)], "scopes": CORE_SCOPES}]}, indent=2))
    adapter_clients = []
    for name, scopes in ADAPTER_CLIENTS.items():
        tok = rnd()
        secret(f"adapter-token-{name}", {"token": write(f"tokens/{name}.token", tok)})
        adapter_clients.append({"client_id": name, "token_sha256": [digest(tok)], "scopes": scopes})
    secret("core-adapter-clients", {"clients.json": write("adapter/clients.json",
                                                         json.dumps({"clients": adapter_clients}, indent=2))})

    # --- databases, broker, object storage ----------------------------------------------------
    for name in ("statements-db-owner", "statements-db-app", "loans-db-owner", "loans-db-app"):
        secret(name, {"password": write(f"db/{name}.password", rnd(24))})
    broker = {}
    for svc, user in (("statements", "nvb_statements"), ("loans", "nvb_loans")):
        broker[user] = rnd(24)
        secret(f"{svc}-broker", {"username": write(f"broker/{svc}.username", user),
                                 "password": write(f"broker/{svc}.password", broker[user])})
    broker["nvb_monitor"] = rnd(24)
    host = args.rabbitmq_host or (f"rabbitmq.{ns}.svc:5672" if args.env == "test" else "REPLACE_RABBITMQ_HOST:5671")
    scheme = "amqps" if (args.rabbitmq_tls or args.env == "poc") else "amqp"
    secret("keda-rabbitmq", {"host": write("broker/keda-host", f"{scheme}://nvb_monitor:{broker['nvb_monitor']}@{host}/nvb")})
    access, key = secrets.token_hex(10).upper(), rnd(30)
    secret("statements-s3", {"access-key": write("s3/access-key", access), "secret-key": write("s3/secret-key", key)})

    if args.env == "test":
        cmds.append("")
        cmds.append("# --- disposable test dependencies and the isolated test core ---")
        secret("core-integration-clients", {"clients.json": core_clients})
        secret("test-postgres-admin", {"password": write("test/postgres-admin.password", rnd(24))})
        secret("core-test-db", {"password": write("test/core-test-db.password", rnd(24))})
        # configure includes nvb.dlx: the relays re-declare the shared dead-letter exchange.
        perms = {"nvb_statements": ("^(nvb\\.statements\\..*|nvb\\.dlx)$","^(nvb\\.statements\\..*|nvb\\.dlx|amq\\.default)$",
                                    "^(nvb\\.statements\\..*|nvb\\.dlx)$"),
                 "nvb_loans": ("^(nvb\\.loans\\..*|nvb\\.dlx)$","^(nvb\\.loans\\..*|nvb\\.dlx|amq\\.default)$",
                               "^(nvb\\.loans\\..*|nvb\\.dlx)$"),
                 "nvb_monitor": ("^$", "^$", "^$")}
        definitions = {
            "vhosts": [{"name": "nvb", "metadata": {"default_queue_type": "quorum"}}],
            "users": [{"name": u, "password_hash": rabbit_hash(pw), "hashing_algorithm": "rabbit_password_hashing_sha256",
                       "tags": ["monitoring"] if u == "nvb_monitor" else []} for u, pw in broker.items()],
            "permissions": [{"user": u, "vhost": "nvb", "configure": c, "write": w_, "read": r}
                            for u, (c, w_, r) in perms.items()],
            # The shared dead-letter exchange is pre-declared so that each service user only needs
            # rights on its own queues (the relays re-declare it idempotently).
            "exchanges": [{"name": "nvb.dlx", "vhost": "nvb", "type": "direct", "durable": True, "auto_delete": False,
                           "internal": False, "arguments": {}}],
        }
        secret("test-rabbitmq-definitions", {"definitions.json": write("test/rabbitmq-definitions.json",
                                                                       json.dumps(definitions, indent=2))})
        secret("test-objectstore-config", {"s3.json": write("test/objectstore-s3.json", json.dumps({"identities": [
            {"name": "nvb-statements", "credentials": [{"accessKey": access, "secretKey": key}],
             "actions": ["Read:nvb-statements", "Write:nvb-statements", "List:nvb-statements", "Admin:nvb-statements"]}]},
            indent=2))})
        seed = "\n".join(f"NVB_SEED_{g}_PASSWORD={rnd(12)}" for g in ("CUSTOMER", "LOADTEST", "DEMO", "ADMIN")) + "\n"
        write("test/core-test-seed.env", seed)
        cmds.append('oc -n "$NS" create secret generic core-test-seed --from-env-file=test/core-test-seed.env')
    else:
        cmds += ["", "# The core's hashed client file (core/integration-clients.json) is NOT a Secret of this namespace:",
                 "# hand it to the core operators for NVB_INTEGRATION_CLIENTS_FILE on the core VMs (docs/CORE_PATCH.md).",
                 "# Database / broker / S3 passwords generated here must be SET on the infra-provided services by",
                 "# their administrators (or replace these files with the values infra issued) before loading."]
    write("create-secrets.sh", "\n".join(cmds) + "\n")
    print(f"credentials written under {out} ({sum(1 for _ in out.rglob('*') if _.is_file())} files). "
          f"Review and run {out / 'create-secrets.sh'}; keep this directory out of version control and delete it "
          "after loading (keep core/integration-clients.json only until it is installed on the core).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
