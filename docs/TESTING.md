# Testing — what ran, what passed, what did not run

Date of the recorded runs: 2026-11-10. Machine: Windows 11 workstation, Python 3.12.10, no
container engine. **Every target was disposable and local** (loopback): nothing connected to the
OpenShift cluster, the core VMs, the core database or any shared service.

"Real" below means the actual server software, started from portable binaries by
`scripts/local_stack.py`: PostgreSQL 16.6, RabbitMQ 3.13.7 (Erlang/OTP 26.2), SeaweedFS 3.97 (S3
API), nginx 1.26.2, and Microsoft Edge (headless) for the browser tests. In-memory doubles are
used only in the unit suites and are never counted as infrastructure validation.

## Results

| # | Suite | Command | Result | Backed by |
| --- | --- | --- | --- | --- |
| 1 | Core regression + patch | `python scripts/safe_core_tests.py --core-dir ../NVB-Monolith-integration/backend` | **185 passed, 3 skipped** (PostgreSQL-only) | SQLite |
| 2 | Core regression + patch | same, `--postgres-url …nvb_disposable_test_core --postgres-password-file … --i-confirm-disposable` | **185 passed, 3 skipped** (the core's own SQLite-file migration tests) | real PostgreSQL |
| 3 | core-adapter unit | `cd services/core-adapter && python -m pytest` | **31 passed** | mocked core transport |
| 4 | statements unit | `cd services/statements && python -m pytest` | **85 passed** | SQLite + in-memory broker/store + fake adapter |
| 5 | loans unit | `cd services/loans && python -m pytest` | **26 passed** | SQLite + in-memory broker + fake adapter |
| 6 | Contract adapter ↔ patched core, in-process E2E | `CORE_BACKEND_DIR=… python -m pytest` | **4 passed** | real core app + real adapter in-process, SQLite |
| 7 | Integration (real dependencies) | `python scripts/run_integration_tests.py` | **40 passed** | real PostgreSQL + RabbitMQ + S3, patched core on PostgreSQL |
| 8 | Gateway over real sockets | `python scripts/local_run.py up` then `python -m pytest tests/gateway/test_gateway.py …` | **12 passed** | real nginx gateway, uvicorn processes, real dependencies |
| 9 | Browser (headless Edge via Playwright) | `python -m pytest tests/gateway/test_browser.py …` | **2 passed** (8 + 9 together: 14 passed) | real browser through the gateway |
| 10 | Image layout + migration commands | `python scripts/verify_image_layout.py --with-postgres` | **4 images OK**; both migration commands succeed from the assembled image tree on real PostgreSQL | not an image build |
| 11 | Manifest validation | `python scripts/validate_manifests.py` | **OK**, 125 rendered objects, both overlays | kustomize 5.4.3 + kubeconform 0.6.7, offline |

Note on 8/9: both need a fresh `python scripts/local_run.py up` (they grant roles and count rows). The installed Edge is newer than the Playwright 1.49 driver; a driver error while closing the browser is ignored in the fixture teardown (it is not a product assertion).

Baseline before this work (same machine): unpatched core 127 passed (SQLite); adapter 30;
statements 56; contract/E2E 4.

### What the suites cover (mapping to the required scenarios)

| Scenario | Where (suite #) |
| --- | --- |
| Expired session; logout | core tests (1, 2); `test_expired_session_is_rejected_by_extension_apis`, `test_logout_ends_the_session…` (8); chain test (7) |
| Invalid / missing / other session's CSRF; cross-origin request | 8, 4, 5, 7 |
| Wrong account owner; cross-customer status and download; unauthorized download | 4, 5, 7, 8 |
| Invalid / missing service credential; wrong scope | core `test_integration_auth` (1, 2); adapter (3); gateway test with a service token (8) |
| Client-supplied identity/role headers ignored | 1, 5, 8 |
| Route precedence vs. the core SPA fallback; internal API not reachable from the browser origin | 8 |
| Duplicate task publication; relay crash after confirm | 4, 7 |
| Worker crash after claim / after upload before DB commit / after DB commit before ack | 4, 5 (in-memory); 7 (real PostgreSQL + RabbitMQ + S3) |
| Worker terminated while processing (hard kill of the process) | `test_worker_killed_mid_run_loses_nothing` (8) |
| Broker outage; unreachable broker; bad broker credentials; publish to a missing queue | 7 |
| Unacked redelivery; dead-lettering; poison message hits the delivery limit; heartbeat during a long task | 7 (real RabbitMQ) |
| Object storage outage; wrong keys; anonymous access denied (private bucket); idempotent re-upload | 7 (real S3 API) |
| Core timeout / unavailability → bounded retry, never a partial statement | 4, 5, 7 |
| Database outage → readiness 503, message not acked | 7 |
| More than 10,000 statement entries | core export tests (1, 2: 10,050 rows paged); render test (4) |
| Concurrent posting during export; back-dated rows; in-place edit with identical totals; delete + re-insert | core consistency tests (1, 2; snapshot tests on PostgreSQL only in 2); worker digest tests (4) |
| Empty statement period; account opened in/after the period; timestamp ties; UTC vs Cairo boundaries | 1, 2, 4, 5 |
| Two reviewers / two approvers updating simultaneously | `test_two_approvers_deciding_simultaneously…`, `test_two_reviewer_updates…` (7, real PostgreSQL, threads) |
| Concurrent idempotent creates, claims, relays, reconcilers, batch advancement | 7 (real PostgreSQL, threads) |
| Migrations: empty → head, 0001 → head with existing rows, downgrade refused, app-role least privilege (audit tables append-only) | 7 |
| Approval posts no money (core ledger/balances unchanged) | 7; source check in 5 |
| Existing core behaviour: authentication, account isolation, sessions across instances, cash, transfers/idempotency, registration/reset | the core's original 127 tests inside 1 and 2 |
| Login through the new origin, cookie attributes, CSP, download in a real browser, staff pages follow extension roles | 8, 9 |

### Things the real-dependency runs found (and fixed)

* A patch test was not re-runnable on PostgreSQL (shared database between tests) — fixed.
* A least-privilege RabbitMQ user needs **write on `amq.default`** to publish through the default
  exchange — now in the provisioning scripts, the credential generator and the contract.
* cmd.exe silently stripped `^` from permission regexes in the local provisioning script —
  arguments are now quoted.
* The worker kept a database transaction open while generating a statement — it now ends the
  read transaction after the claim (the lease heartbeat could not run otherwise).
* The gateway's Content-Security-Policy blocks `eval` — confirmed by the browser test tooling.

## Not run — and why

| Item | Why | What is needed |
| --- | --- | --- |
| Container image builds | no podman/docker/WSL on the workstation | runbook step 5 on a machine with podman |
| Anything on OpenShift (apply, Routes, service-serving certificates, NetworkPolicies, SCC behaviour, KEDA) | not authorised and not possible from here; no `oc` | runbook Part A in the isolated namespace |
| TLS between components | local stack uses plain HTTP on loopback with explicit development flags; the TLS code paths (CA + hostname verification) are unit-level only | first cluster run (runbook step 8 exercises them) |
| RabbitMQ restart persistence, multi-node quorum behaviour | single local node; restart test not automated | optional drill in runbook step 16 |
| Per-service RabbitMQ permission regexes | local user uses `^nvb\..*` | first cluster run |
| Load tests | script exists (`tests/load`); only ad-hoc laptop use; no numbers recorded | runbook step 16, with infra |
| `deploy/local/compose.yaml` | no container engine | any machine with podman compose |
| Unpatched core suite on PostgreSQL | not needed for the patch decision; only the SQLite baseline (127) was recorded | optional |

## Safety rules for tests

* The core's own fixture drops the `public` schema of whatever `NVB_TEST_DATABASE_URL` points at.
  Only `scripts/safe_core_tests.py` is used; it strips inherited `NVB_/ADAPTER_/STATEMENTS_/
  LOANS_/GATEWAY_/PG*` variables and refuses unless: the host is allow-listed, the database name
  starts with `nvb_disposable_test_`, the role is not a superuser, **and the live database carries
  the comment `nvb-disposable-test`** (verified by connecting before pytest starts).
  The word "safe" in the name is backed by those checks, not assumed.
* `tests/integration` and `scripts/local_run.py` apply the same four checks before any reset.
* Credentials are generated per environment/run into git-ignored directories and passed by file.
