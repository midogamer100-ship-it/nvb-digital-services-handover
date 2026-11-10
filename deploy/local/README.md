# Local disposable development / test setup

Two equivalent ways to get real PostgreSQL, RabbitMQ and S3-compatible storage for tests:

1. **No container engine (what the authors used, Windows)** — `scripts/local_stack.py`:
   portable PostgreSQL 16.6, Erlang/OTP 26 + RabbitMQ 3.13.7, SeaweedFS 3.97 and nginx 1.26
   under `.local/` (git-ignored), checksummed downloads, loopback only, non-default ports, random
   credentials in `.local/secrets/`. `fetch`, `start`, `status`, `env`, `stop`, `destroy`.
2. **podman / docker** — `compose.yaml` in this directory (same ports and names; written for
   teams with a container engine, **not executed by the authors**). Create the three databases,
   the application roles, the vhost/user and the bucket as `local_stack.py` does, and mark each
   test database: `COMMENT ON DATABASE nvb_disposable_test_x IS 'nvb-disposable-test'`.

Then:

* `python scripts/run_integration_tests.py` — integration suite against those dependencies.
* `python scripts/local_run.py up` — the whole stack over real sockets with a real nginx gateway
  (portal on http://127.0.0.1:18080), used by `tests/gateway` and for manual demos.

Safety: every runner strips inherited `NVB_/ADAPTER_/STATEMENTS_/LOANS_/GATEWAY_/PG*` variables and
refuses databases that are not on an allow-listed host, not named `nvb_disposable_test_*`, not
marked with the disposable comment, or reached with a superuser role. `local_run.py up` RESETS the
three local test databases.
