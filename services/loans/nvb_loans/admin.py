"""Role administration command for the loans service.

    python -m nvb_loans.admin grant --subject nvb-user:12:1730000000 --role extension_admin \
        --actor "ops:<ticket-or-name>" --reason "bootstrap"
    python -m nvb_loans.admin list

Roles of this service: reviewer, approver, batch_operator, extension_admin. See nvb_platform.role_cli.
"""

from __future__ import annotations

import sys

from nvb_loans.config import get_settings
from nvb_loans.db import build_engine
from nvb_loans.models import KNOWN_ROLES, RoleAssignment, RoleEvent
from nvb_platform.role_cli import run


def main(argv: list[str] | None = None) -> int:
    return run(argv, settings=get_settings(), known_roles=KNOWN_ROLES, assignment_model=RoleAssignment,
               event_model=RoleEvent, engine_name="nvb-loans-admin", prog="loans-admin", build_engine=build_engine)


if __name__ == "__main__":
    sys.exit(main())
