"""Operator command for explicit extension role administration (bootstrap and break-glass).

The first ``extension_admin`` of a service cannot be granted through its API (nobody holds the
role yet), so it is granted by this command, run as a reviewed one-off Job with that service's
database credentials. Every change is written to the append-only ``role_events`` table.
The subject is the stable core identity shown on the portal's "My identity" panel.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys

from nvb_platform import roles as roles_lib
from nvb_platform.db import ConfigError, build_sessionmaker
from nvb_platform.logging_setup import configure_logging


def run(argv: list[str] | None, *, settings, known_roles, assignment_model, event_model, engine_name: str,
        prog: str, build_engine) -> int:
    p = argparse.ArgumentParser(prog=prog)
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("grant", "revoke"):
        c = sub.add_parser(name)
        c.add_argument("--subject", required=True)
        c.add_argument("--role", required=True, choices=sorted(known_roles))
        c.add_argument("--actor", required=True, help="who performs the change, e.g. ops:<name or ticket>")
        c.add_argument("--reason", default="")
        c.add_argument("--label", default="", help="display label only (e.g. the username)")
    sub.add_parser("list")
    args = p.parse_args(argv)
    configure_logging(settings.log_level, prog, f"{socket.gethostname()}:{os.getpid()}")
    try:
        sm = build_sessionmaker(build_engine(settings, engine_name))
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 78
    with sm() as db:
        if args.command == "list":
            print(json.dumps(roles_lib.list_assignments(db, assignment_model), indent=2))
            return 0
        try:
            changed = roles_lib.set_role(db, assignment_model, event_model, subject=args.subject, role=args.role,
                                         active=args.command == "grant", actor=args.actor, known_roles=known_roles,
                                         reason=args.reason, label=args.label)
        except roles_lib.RoleError as exc:
            print(f"rejected: {exc.code}: {exc.message}", file=sys.stderr)
            return 2
        db.commit()
        print(json.dumps({"changed": changed, "subject": args.subject, "role": args.role,
                          "active": args.command == "grant"}))
    return 0
