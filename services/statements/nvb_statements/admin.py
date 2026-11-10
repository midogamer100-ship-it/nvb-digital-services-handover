"""Role administration command for the statements service.

    python -m nvb_statements.admin grant  --subject nvb-user:12:1730000000 --role extension_admin \
        --actor "ops:<ticket-or-name>" --reason "bootstrap"
    python -m nvb_statements.admin revoke --subject ... --role batch_operator --actor ... --reason ...
    python -m nvb_statements.admin list

Roles of this service: batch_operator, extension_admin. See nvb_platform.role_cli.
"""

from __future__ import annotations

import sys

from nvb_platform.role_cli import run
from nvb_statements.config import get_settings
from nvb_statements.db import build_engine
from nvb_statements.models import KNOWN_ROLES, RoleAssignment, RoleEvent


def main(argv: list[str] | None = None) -> int:
    return run(argv, settings=get_settings(), known_roles=KNOWN_ROLES, assignment_model=RoleAssignment,
               event_model=RoleEvent, engine_name="nvb-statements-admin", prog="statements-admin",
               build_engine=build_engine)


if __name__ == "__main__":
    sys.exit(main())
