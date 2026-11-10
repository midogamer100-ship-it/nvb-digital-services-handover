# nvb-digital-services

Containerised extension services around the NVB demo core banking application for an OpenShift
modernisation proof of concept: **PDF account statements, loan applications (workflow only) and
batch processing**. The core keeps running on its VM pool and stays authoritative for customers,
accounts, balances and every financial posting.

**Synthetic demo data only. No disbursement, no real messaging, no production banking.**
Local repository only: nothing has been pushed, published or deployed.

## Readiness — stated precisely

| Achievement | Status |
| --- | --- |
| Code implemented | **Yes** — core adapter, statements (api/relay/worker/batch), loans (api/relay/worker/batch), gateway, portal, migrations, core patch |
| Local integration tested | **Yes** — against real, disposable PostgreSQL 16, RabbitMQ 3.13 and an S3-compatible store, a patched core on PostgreSQL, real nginx and a real browser (results: `docs/TESTING.md`) |
| Manifests validated | **Yes, offline** — kustomize build + kubeconform schema + policy checks (`scripts/validate_manifests.py`) |
| Image built | **No** — no container engine on the authoring workstation. Containerfiles are verified structurally and by running every image command from the assembled layout (`scripts/verify_image_layout.py`) |
| OpenShift deployment tested | **No** — nothing was applied to any cluster |

Therefore this is **ready for an isolated OpenShift test deployment to be attempted** by the
infrastructure team following `docs/RUNBOOK.md`; it is not "deployed" or "validated on OpenShift".
Open items: `docs/BLOCKERS.md`.

## Components

| Path | What | State |
| --- | --- | --- |
| `services/core-adapter` | Only component that calls the core's `/internal/v1`; bounded concurrency/rate; validates the contract | implemented, tested |
| `services/statements` | One image, several commands: `statements-api`, `statements-relay` (outbox → RabbitMQ, reconciler), `statements-worker` (PDF), batch command, role command, migrations | implemented, tested |
| `services/loans` | One image: `loans-api`, `loans-relay`, `loans-worker` (synthetic summary), batch command, role command, migrations | implemented, tested |
| `libs/nvb_platform` | Shared library: broker, outbox, adapter client, dataset verification, roles, browser identity | implemented, tested through the services |
| `gateway/` | nginx same-origin gateway (config template + renderer) | implemented, tested with real nginx |
| `portal/` | Static single-page portal (no build step, no third-party JavaScript): customer and staff pages | implemented, browser-tested |
| `migrations/{statements,loans}` | Alembic migrations, shipped inside the service images | tested on PostgreSQL (fresh install and upgrade path) |
| `core-patch/` + `docs/CORE_PATCH.md` | Additive core patch `ffd3201 → 09730f5` as a patch series, with rollout/rollback | tested on SQLite and PostgreSQL; not deployed |
| `deploy/openshift` | kustomize base, `overlays/test` (disposable namespace + throw-away dependencies + isolated test core), `overlays/poc`, Jobs, CronJobs, optional KEDA ScaledJobs, secret templates | validated offline; never applied |
| `deploy/local`, `scripts/local_stack.py`, `scripts/local_run.py` | Disposable local dependencies and the full stack over real sockets (no containers needed) | used for all integration tests |
| `contracts/` | Core integration contract, extension API / adapter / queue contracts | current |
| `tests/` | `contract`, `e2e` (in-process), `integration` (real dependencies), `gateway` (real nginx + browser), `load` (finite, guarded) | see `docs/TESTING.md` |

## Principles

* New services never touch core tables; they call the core only through the adapter with scoped,
  hashed service credentials over verified TLS.
* Identity = the core session, verified at the core on every request. Staff capabilities are
  explicit extension role assignments; core roles grant nothing.
* Loan approval is a workflow decision. Nothing posts money.
* Money is integer minor units end to end.
* Images are immutable releases; containers never pull code. Secrets are mounted files.
* No statement is ever generated from a partial or drifting dataset.

## Quick start (laptop, Windows, no containers)

```
python -m venv .venv && .venv\Scripts\pip install -r services/statements/requirements-dev.txt -r services/loans/requirements-dev.txt pyyaml playwright
python scripts/local_stack.py fetch     # portable PostgreSQL / Erlang+RabbitMQ / SeaweedFS / nginx (checksummed)
python scripts/local_stack.py start     # disposable, loopback only, random credentials in .local/secrets
python scripts/local_run.py up          # patched core + adapter + statements + loans + relays + gateway
# portal: http://127.0.0.1:18080   demo passwords: .local/run/demo-passwords.json
python scripts/local_run.py worker statements
python scripts/local_run.py down && python scripts/local_stack.py stop
```
With podman/docker instead: `deploy/local/compose.yaml` (dependencies only; not run by the authors).

## Documents

`docs/RUNBOOK.md` (deployment order, rollback, troubleshooting, cutover) ·
`docs/CONFIGURATION.md` (every variable, secrets, TLS, rotation) · `docs/CORE_PATCH.md` ·
`docs/DESIGN.md` (gateway, consistency, roles, durability, scaling) · `docs/TESTING.md` ·
`docs/DEMO_SCRIPT.md` · `docs/BLOCKERS.md` · `contracts/*.md` · `deploy/openshift/secrets/README.md`
