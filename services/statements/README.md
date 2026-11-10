# services/statements

Package `nvb_statements`, one image (`services/statements/Containerfile`, build context =
repository root), several commands:

| Component | Command | Kind |
| --- | --- | --- |
| statements-api | `uvicorn --factory nvb_statements.api:app_factory` (image default) | Deployment + Service |
| statements-relay | `python -m nvb_statements.relay` | Deployment (required: publishes the outbox, declares queues, reconciles) |
| statements-worker | `python -m nvb_statements.worker --drain` | Job / KEDA ScaledJob (bounded finite run) |
| batch trigger | `python -m nvb_statements.batch create --period previous-month --trigger schedule` | CronJob (same operation as the staff API) |
| role administration | `python -m nvb_statements.admin grant|revoke|list` | one-off Job |
| migration | `alembic -c /opt/app/migrations/statements/alembic.ini upgrade head` | one-off Job |

API: `contracts/extension-apis.md`. Configuration: `docs/CONFIGURATION.md`. Design: `docs/DESIGN.md`.
Tests: `python -m pytest` here (SQLite + in-memory doubles), `tests/integration` (real dependencies).
