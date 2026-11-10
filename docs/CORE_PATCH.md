# Core integration patch (`/internal/v1`) — package for the core operators

The extension services need five authenticated, read-only endpoints on the core. They are an
**additive, disabled-by-default** patch. Nothing in this repository deploys it.

## Identification

| Item | Value |
| --- | --- |
| Core repository (isolated clone) | `NVB-Monolith-integration` (clone of `NVB-Monolith`; its push URL is disabled on purpose) |
| Branch | `feature/extension-integration-api` |
| Base revision | `ffd32015aa00564054bb177f6580196ce1b1355c` (`main` of `NVB-Monolith`, identical to `NVB-Monolith.zip`) |
| Target revision | `09730f5` (two commits: `978ba7b` API + auth, `09730f5` statement-v2 dataset) — confirm with `git rev-parse feature/extension-integration-api` |
| Reviewable patch series | `core-patch/0001-*.patch`, `core-patch/0002-*.patch` in this repository (`git format-patch ffd3201..09730f5`); apply with `git am` on a branch created from the base |
| Deployed core revision on the VMs | **unknown to us** — must be confirmed by infra before any rollout (see "Rollout") |

Files changed (9 files, +1753 / −1):

```
backend/app/api/integration.py                 new   router, client registry, scopes, bounded export slots
backend/app/services/integration_exports.py    new   read-only queries, subject, manifest + content digest
backend/app/config.py                          +19   settings (all default to "off"/safe values)
backend/app/main.py                            +6    router registered ONLY when NVB_INTEGRATION_ENABLED=true
backend/tests/integration_support.py           new   test helpers
backend/tests/test_integration_auth.py         new   26 tests
backend/tests/test_integration_exports.py      new   23 tests
backend/tests/test_integration_consistency.py  new   13 tests (3 PostgreSQL-only)
backend/tests/test_health.py                   ±5    test-only fix of a flaky assertion (label values only)
```

**No schema change. No Alembic migration.** Evidence: `git diff --stat ffd3201 09730f5 -- backend/alembic
backend/app/models.py backend/app/db.py backend/requirements.txt` is empty. Existing routes,
`require_customer`, `owned_account`, money handling, transfer/cash idempotency and the role
constraint are untouched (no existing file other than `config.py`, `main.py` and one test is modified).

## Endpoints (contract: `contracts/core-integration-v1.md`)

```
POST /internal/v1/sessions/introspect                    scope identity:introspect (+accounts:read for include_accounts)
GET  /internal/v1/subjects/{subject}/accounts            scope accounts:read
GET  /internal/v1/accounts/{id}/statement-manifest       scope statements:extract or summaries:read
GET  /internal/v1/accounts/{id}/statement-entries        scope statements:extract
GET  /internal/v1/discovery/statement-accounts           scope accounts:discover
```

## New environment variables (prefix `NVB_`)

| Variable | Default | Meaning |
| --- | --- | --- |
| `NVB_INTEGRATION_ENABLED` | `false` | Feature flag. `false` ⇒ the router is not imported or registered; `/internal/*` behaves exactly as before the patch |
| `NVB_INTEGRATION_CLIENTS_FILE` | unset | Path of the hashed client file. Enabled without a readable, valid file ⇒ every call is rejected with 401 |
| `NVB_INTEGRATION_MAX_CONCURRENT_EXPORTS` | `2` | Per core PROCESS bound on concurrent manifest/entries queries (429 + `Retry-After` beyond it) |
| `NVB_INTEGRATION_EXPORT_QUEUE_TIMEOUT_SECONDS` | `2` | How long a request waits for an export slot |
| `NVB_INTEGRATION_MAX_PAGE_SIZE` | `1000` | Upper bound of `limit` for entries |
| `NVB_INTEGRATION_MAX_PERIOD_DAYS` | `400` | Longest period |
| `NVB_INTEGRATION_SETTLE_SECONDS` | `300` | A period must have ended at least this long ago |
| `NVB_INTEGRATION_DISCOVERY_MAX_PAGE_SIZE` | `500` | Upper bound of `limit` for discovery |
| `NVB_INTEGRATION_MAX_STATEMENT_ENTRIES` | `250000` | Rows hashed by one manifest; above it `422 dataset_too_large` (never truncated) |

The extension side must agree with these: `STATEMENTS_SETTLE_SECONDS`/`LOANS_SETTLE_SECONDS` ≥ settle,
`STATEMENTS_ENTRIES_PAGE_SIZE` ≤ `ADAPTER_ENTRIES_MAX_PAGE_SIZE` ≤ max page size,
`STATEMENTS_MAX_ENTRIES_PER_STATEMENT` ≤ max statement entries (`scripts/validate_manifests.py` checks this).

## Service credential file (`NVB_INTEGRATION_CLIENTS_FILE`)

JSON, readable by the core service user only (e.g. `0440 root:nvb`). It contains **SHA-256 digests**,
never raw tokens:

```json
{"clients": [
  {"client_id": "nvb-core-adapter",
   "token_sha256": ["<hex digest of the current token>", "<hex digest of the next token during rotation>"],
   "scopes": ["identity:introspect", "accounts:read", "statements:extract", "summaries:read", "accounts:discover"]}
]}
```

`scripts/generate_credentials.py` writes this file as `core/integration-clients.json` together with
the matching raw token for the adapter Secret. The file is re-read when its mtime changes (checked
at most every 15 s), so rotation needs no core restart. Unknown scopes, a malformed file or a
missing file fail closed (all requests 401).

## Verified behaviour (tests in the patch; results in `docs/TESTING.md`)

* Missing / malformed / too-short / unknown bearer ⇒ 401; valid token without the scope ⇒ 403.
* Kubernetes ServiceAccount tokens, session cookies or client tokens do not open customer APIs,
  and a client token does not open `/api/*` (`test_client_token_does_not_grant_customer_api_access`).
* Network placement is not the control: the bearer check runs on every request.
* Raw tokens are never logged: the code logs client ids and error types only; the audit rows of
  manifest/discovery calls contain client id, account id, period and counts (asserted by
  `test_manifest_is_audited_without_sensitive_data`). The introspection request body (which carries
  a session token) is not logged by the core's request logger (it logs method, route template,
  status, duration).
* Introspection uses the core's own `load_session`: idle/absolute expiry, logout, password reset
  and user suspension behave exactly as for a browser request. It never returns the CSRF secret,
  only `csrf_valid` for a presented token.
* Disabled by default: with default settings no `/internal` route exists
  (`test_integration_disabled_by_default_registers_nothing`).

One operational fact to act on: the VM nginx (`deploy/vm/nginx-nvb.conf`) forwards every path
except `/metrics`, so once enabled, `/internal/v1` is reachable through the existing core route
(still behind the bearer check). Recommended hardening, owned by infra: allow `/internal/` only
from the adapter's egress address at the VM nginx or HAProxy.

## Regression results

| Suite | Unpatched base `ffd3201` | Patched `09730f5` |
| --- | --- | --- |
| Core suite on SQLite | 127 passed | 185 passed, 3 skipped (PostgreSQL-only) |
| Core suite on **disposable PostgreSQL 16** | not re-run by us | 185 passed, 3 skipped (the core's own SQLite-only migration tests) |

The 127 original tests (authentication, account isolation, sessions across instances, cash
operations, transfers/idempotency, registration/reset, money, health, migrations) are all part
of the 185 and pass unchanged, apart from the one test-only assertion fix in `test_health.py`.
Run them yourself with the guarded runner (refuses anything that is not a marked disposable database):

```
python scripts/safe_core_tests.py --core-dir ../NVB-Monolith-integration/backend \
  --postgres-url postgresql+psycopg://<role>@<disposable-host>:5432/nvb_disposable_test_core \
  --postgres-password-file <file> --allow-host <disposable-host> --i-confirm-disposable
```

## Activation / deactivation

1. Install the code revision (flag still `false`): behaviour is identical to the base.
2. Place the clients file; set `NVB_INTEGRATION_CLIENTS_FILE`.
3. Set `NVB_INTEGRATION_ENABLED=true` and restart the core service (the flag is read at start).
4. Verify from the adapter side (runbook step 8). Deactivate by setting the flag back to `false`
   and restarting: the routes disappear; no data needs cleaning (the patch writes only audit rows).

## Rollout to the VM fleet — to be executed by infra, not by us

The team reports that VMs fetch code from Git at start/update. Therefore **pushing this branch to
the branch the VMs follow would roll it out implicitly**. Do not do that. Proposed sequence:

1. Confirm the revision the VMs run today and which Git ref/bootstrap script they follow.
2. Stage: build/run ONE isolated core instance (not in the pool) from `09730f5` against a
   disposable database; run the regression suite above and runbook steps 7–12 against it.
3. Agree the release ref (e.g. a tag `core-integration-v1` on the reviewed commit). Merge to the
   followed branch only in a change window, with the flag `false` everywhere (inert code).
4. Canary: enable the flag + clients file on one pool VM that HAProxy can drain; verify
   `/internal/v1` with the adapter's token and that customer traffic is unaffected.
5. Roll the flag to the remaining VMs. All VMs must be on the patched revision **before** the
   extension namespace is pointed at the pool (the adapter may hit any VM).
6. Future clones: the golden image / bootstrap must resolve to the approved ref, and the
   clients file + the two env vars must be part of the VM configuration source (env file or
   config management), otherwise a new clone starts with the flag off (safe, but the adapter
   then gets 404 from that VM ⇒ `core_error`). We do not modify the golden image.

Rollback: set `NVB_INTEGRATION_ENABLED=false` and restart (immediate, per VM), or redeploy the base
revision. There is no schema migration to reverse. Extension requests in flight fail with
retryable errors and resume when the flag returns.
