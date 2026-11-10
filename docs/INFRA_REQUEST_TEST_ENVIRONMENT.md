# Request to infrastructure: disposable test environment for nvb-digital-services

Purpose: run PostgreSQL / RabbitMQ / S3 integration tests for the extension services and the
core integration patch **without touching the core, the shared demo database or `bank-poc`**.
Everything below is disposable: it may be wiped at any time and must contain only synthetic data.

## Requested resources

| Item | Requirement | Why |
| --- | --- | --- |
| Namespace | New, e.g. `nvb-digital-test`. Not `bank-poc`. No access to core VMs or core DB from it. | Isolation |
| PostgreSQL 16 | Separate instance (not the core DB VM `10.11.11.101`, not a database on it). | Core suite drops `public` schema |
| Databases | `nvb_disposable_test_core` (core suite + patch), `nvb_disposable_test_statements`, `nvb_disposable_test_loans` | Guarded runner requires the `nvb_disposable_test_` prefix |
| Roles | One test owner role per DB (may DROP/CREATE within its DB only), no superuser, no access to other DBs | Least privilege |
| RabbitMQ 3.13+ | Dedicated vhost `nvb-test`, user with configure/write/read on that vhost only; quorum queues allowed; management API read access optional | Durable queues, publisher confirms, DLX tests |
| S3-compatible storage | Dedicated private bucket `nvb-test-statements` (no public policy), access key scoped to that bucket, versioning off | PDF upload / crash-reconciliation tests |
| CA bundle | PEM for the endpoints' TLS certificates | We never disable TLS verification |
| Access path | From the developer workstation (port-forward / VPN) **or** a test Job in the test namespace that runs the suites | Either works |
| Capacity | Small: ~0.5 CPU / 1 GiB total is enough; single node, so please cap it | Shared physical node with core VMs |

## Credential handover

* Deliver credentials as files (or a Secret in the test namespace), not in chat/email/tickets.
* We reference them only via env vars that point at files (`*_FILE`) or via a local `.env` that is git-ignored.
* Test credentials only; please rotate/delete after the test window.

## What we will run

* `scripts/safe_core_tests.py --postgres-url … --allow-host <test-pg-host> --i-confirm-disposable`
  (the host must be added explicitly; database name must start with `nvb_disposable_test_`).
* Extension integration suites (`tests/integration`) — they refuse to run unless
  `NVB_DIGITAL_TEST_ENV=disposable` is set **and** the DB name has the disposable prefix.
* Finite, bounded local load tests only. No cluster load tests without a separately reviewed plan.

## What we will NOT do

Apply manifests to `bank-poc`, touch the VM pool/HPA/golden image/HAProxy/Route, install operators,
change RBAC, or connect to the core database.

## Questions for infra (same list as M1)

1. Deployed core revision and whether the additive `/internal/v1` patch may be deployed (disabled first).
2. Canonical new portal hostname and gateway type (HTTP-aware, TLS edge or re-encrypt).
3. Service-to-service auth: bearer client credentials only, or mTLS available?
4. Installed KEDA version (ScaledJob API) and allowed namespace for `nvb-digital`.
5. Object storage product/endpoint for the real PoC namespace.
