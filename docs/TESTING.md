# Testing

All tests run locally against disposable state only. Nothing here connects to the cluster.

## Commands

```bash
# 1. Core suite incl. integration patch (SQLite, sanitized env)
.venv/Scripts/python scripts/safe_core_tests.py --core-dir ../NVB-Monolith-integration/backend -- -q -p no:cacheprovider

# 2. Core adapter unit tests
cd services/core-adapter && ../../.venv/Scripts/python -m pytest

# 3. Statements api/relay/worker (SQLite, in-memory broker/store, fake adapter)
cd services/statements && ../../.venv/Scripts/python -m pytest

# 4. Contract + E2E: real adapter and statements stack against the patched core (in-process, SQLite)
CORE_BACKEND_DIR=../NVB-Monolith-integration/backend .venv/Scripts/python -m pytest
```

`scripts/test-all.sh` runs all four and reports a non-zero exit if any fails.

## Results (2026-11-10, Windows 11, Python 3.12.10)

| Suite | Result | Backend |
| --- | --- | --- |
| Core baseline (unpatched) | 127 passed | SQLite |
| Core + integration patch | 175 passed, 6/6 consecutive runs | SQLite |
| core-adapter unit | 30 passed | mocked core |
| statements (api, relay, worker, period, render, migration) | 56 passed | SQLite + in-memory broker/store + fake adapter |
| contract adapter ↔ core | 2 passed | in-process core, SQLite |
| E2E statement (session → API → outbox → worker → adapter → core → PDF → download) | 2 passed | in-process, SQLite, in-memory broker/store |

### Core test fix included in the patch

`backend/tests/test_health.py::test_metrics_labels_use_route_templates_not_ids` asserted that `999999`
does not appear anywhere in `/metrics`. Float sample values (e.g. `0.011107199999969453`) sometimes contain
it; with the 48 extra tests this false positive hit 2/6 runs (original core: 0/6). The assertion now checks
label values only, which is what the test intends. Test-only change; no application behaviour changed.

## Not run (and why)

* **PostgreSQL** runs of the core suite and patch: no container runtime on this workstation; infra will provide
  a disposable environment (request: `docs/INFRA_REQUEST_TEST_ENVIRONMENT.md`). Use `--allow-host <host>`.
  SQLite (BEGIN IMMEDIATE) does not prove REPEATABLE READ snapshot behaviour, `FOR UPDATE` locking or
  concurrent-posting semantics. Required before M2 sign-off:
  `scripts/safe_core_tests.py --postgres-url postgresql+psycopg://…@localhost/nvb_disposable_test_core --i-confirm-disposable`
* Real RabbitMQ (confirms, quorum queue, DLX), real S3, PostgreSQL `SKIP LOCKED`/conditional-update races:
  code exists (`RabbitBroker`, `S3Store`), not executed — waiting for the infra test environment.
* Gateway, React portal, browser login/logout through the new origin: not built yet.
* Load tests: not built yet (M3).
* Frontend build of the core: not needed for these tests.

## Safety rules for tests

* Never set `NVB_TEST_DATABASE_URL` to the core or shared demo database — the core fixture drops the schema.
* The guarded runner strips every inherited `NVB_*` variable and refuses non-allow-listed PostgreSQL hosts.
* Test credentials are generated per run (`secrets.token_urlsafe`) and never written outside `tmp_path`.
