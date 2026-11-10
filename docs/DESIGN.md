# Design decisions (what was built and why)

## 1. Boundaries

```
browser ──TLS──> Route (re-encrypt, NEW hostname) ──> gateway (nginx + static portal)
                                                       ├─ /api/auth/{login,logout,me} ──TLS+verify──> core LB (existing, unchanged)
                                                       ├─ /x/statements/* ──> statements-api ─┐
                                                       └─ /x/loans/*      ──> loans-api ──────┤ bearer service credential
                                                                                             ▼
   statements-worker / loans-worker (Jobs) ─────────────────────────────────────────> core-adapter (1 replica)
                                                                                             │ bearer client credential, TLS+verify
                                                                                             ▼
                                                                          core /internal/v1 (read-only, feature-flagged)
   statements DB · loans DB (PostgreSQL)     RabbitMQ (outbox relays publish)     S3 bucket (private)
```

* The core stays authoritative for customers, accounts, balances and every financial posting.
  The extension has **no core database credentials and no code path that writes to the core**
  (the adapter client exposes only identity, accounts, manifest, entries and discovery reads).
* Each service owns its database (separate databases and roles). No cross-service calls except
  through the adapter. Shared code is a library (`libs/nvb_platform`), not a shared service.
* Loan "approval" is a row state. Verified by a test that compares the core ledger/balances
  before and after an approval on real PostgreSQL.

## 2. Same-origin gateway (portal, core login, extension APIs)

* New origin = new Route hostname with **re-encrypt** TLS to an nginx gateway pod. The existing
  core Route (passthrough) and HAProxy are not modified; passthrough cannot route by path, which
  is why a separate HTTP-aware hop exists.
* The gateway proxies exactly three core paths (`/api/auth/login|logout|me`) so the browser logs
  in with the existing core credentials. The core sets `nvb_session` without a `Domain`
  attribute, so the cookie becomes host-only for the portal hostname: no cookie-domain or CORS
  workaround, and a session of the old core hostname is not valid on the portal (and vice versa).
* Extension APIs never trust the gateway for identity: they take the cookie value and ask the
  core (through the adapter) on every request. The gateway additionally strips client-supplied
  identity/role/`Authorization` headers, refuses cross-origin state-changing requests (Origin
  check) and rate-limits login per client address.
* CSRF: the core's per-session token (returned by login/me) is sent as `X-CSRF-Token`; the core
  validates it for core calls, and for extension calls via introspection (`csrf_valid`).
* Route precedence is an explicit allowlist; unknown `/api`, `/x`, `/internal`, `/health`,
  `/metrics` paths return JSON 404 at the gateway, so neither the core's SPA fallback nor the
  portal's `index.html` can answer for an API path. Tested against real nginx.
* Known limitation: the core sees the gateway as the client address (login throttling per IP is
  shared by portal users; audit rows show the gateway address). The gateway sends
  `X-Forwarded-For`; whether the core trusts it is a core/VM nginx setting we did not change.
  The per-user throttle of the core is unaffected.

## 3. Statement consistency

See `contracts/core-integration-v1.md` ("What consistent means here"). Summary: option B of the
brief with a stronger mechanism than count/total comparison — a SHA-256 over every entry computed
in the core's single manifest snapshot, recomputed by the worker over the paged entries, plus a
second manifest read. No core transaction spans requests; rendering happens outside the core. A
mismatch is retried with backoff (bounded) and never rendered. The PDF states the period in local
time and UTC, the dataset id and "balances as computed from the core ledger for the stated period".

## 4. Extension roles

Role assignments are rows `(subject, role, active)` in **each service's own database**, with an
append-only `role_events` history; the application database role cannot UPDATE/DELETE history
(verified on PostgreSQL). The subject is the core's stable identity
(`nvb-user:<id>:<created>`), verified through introspection. Core roles grant nothing.
Statements knows `batch_operator`, `extension_admin`; loans knows `reviewer`, `approver`,
`batch_operator`, `extension_admin`. The first admin of each service is granted by a one-off Job
(`python -m nvb_<service>.admin grant …`); afterwards the portal **Roles** page is used.
Consequence to know: roles are per service — a loans approver is not a statements batch operator.

Loans rules enforced server-side: ownership (customer), assignment (reviewer sees/acts only on
assigned applications), separation (decision by an approver who is neither the reviewer nor the
applicant; reviewers cannot self-assign), valid transitions, optimistic `version`, mandatory
reasons, attributed events with timestamps.

## 5. Durability model (statements and loans)

* **Outbox**: the business row, its audit event and the outbox message commit together; the relay
  publishes with broker confirms and marks rows afterwards (`FOR UPDATE SKIP LOCKED`).
* **At-least-once + idempotent results**: conditional claim (`queued` & due, or `running` with an
  expired lease); deterministic object key; completion conditional on the lease owner; terminal
  states are acked without work.
* **Leases**: claimed for `*_LEASE_SECONDS`, renewed by a heartbeat thread every third of that
  while the task runs; the relay re-queues expired leases, due retries and stale queued rows.
* **Crash matrix** (each covered by tests on real PostgreSQL/RabbitMQ/S3):

| Crash / outage | Effect | Recovery |
| --- | --- | --- |
| after claim, before work | row `running`, message unacked | redelivery waits; lease expires → relay re-queues |
| after upload, before DB commit | object exists, row `running` | retry writes the same key; exactly one object |
| after DB commit, before ack | row `completed`, message redelivered | consumer sees terminal state, acks |
| relay after confirm, before mark | duplicate message | idempotent handler |
| broker down | outbox rows stay unpublished | relay reconnects and publishes |
| object storage down | `retry_scheduled` (backoff), then `failed` after `*_MAX_ATTEMPTS` | no partial result, status never `completed` |
| core/adapter down or timing out | same bounded retry | never a partial statement |
| database down | worker requeues the message and exits non-zero; API readiness 503 | Job restarts later |
| worker killed (SIGKILL) | message redelivered, lease expires | next bounded run completes |
| poison message | `x-delivery-limit` → dead-letter queue | operator inspects |

## 6. Worker execution and autoscaling — one model

* `--drain` is a **bounded finite run**: one message at a time, at most `*_WORKER_MAX_MESSAGES`
  (50) messages or `*_WORKER_MAX_RUNTIME_SECONDS`, exit 0 after `*_IDLE_EXIT_SECONDS` of empty
  queues, finish the message in hand on SIGTERM. It is neither "one message per Job" nor "drain
  the whole backlog".
* The statements worker polls the interactive queue first, then the batch queue: a large batch
  cannot starve a customer's on-demand request.
* Manual path: a Job from `jobs/worker-*`. Autoscaling path: KEDA `ScaledJob` with
  `queueLength` target **50 = messages per Job**, strategy `accurate` (the AMQP scaler counts
  ready messages only), `maxReplicaCount` 2 (statements) / 1 (loans). So Jobs ≈
  ceil(ready / 50), capped. A Job's `activeDeadlineSeconds` (1500 s) exceeds its runtime bound
  (900 s) plus one task; a Job cut off anyway is covered by the crash matrix.
* No HPA anywhere; the ScaledJobs are the only autoscalers (enforced by the manifest validator).

### Fleet-wide protection of the core

| Layer | Bound | Where |
| --- | --- | --- |
| Concurrent core calls from the whole extension | adapter replicas (1) × `ADAPTER_CORE_MAX_CONCURRENCY` (4) = **4** | enforced by `validate_manifests.py` against the annotation `nvb.io/core-fleet-max-concurrency` |
| Core request rate from the extension | 1 × `ADAPTER_CORE_RATE_PER_SECOND` (20/s) | adapter token bucket |
| Expensive export queries per core process | `NVB_INTEGRATION_MAX_CONCURRENT_EXPORTS` (2), 429 beyond | core patch |
| Workers | ≤ 2 statement Jobs + 1 loans Job, one adapter call at a time each | KEDA caps / manual Jobs |

Per-replica limits multiply with replicas, which is why the adapter is a single replica with
`RollingUpdate maxSurge 1` (briefly 2 pods ⇒ briefly 8) and has no autoscaler. Raising any of
these numbers is a capacity decision to take with measurements from runbook step 16.

## 7. Batch orchestration

Statements: `freezing` (page the core's authorised discovery into `batch_items`, cursor persisted
per page) → `releasing` (create requests + outbox rows on the batch queue in chunks) → `running`
→ `completed` / `completed_with_failures`. Nothing is processed before the manifest is frozen;
every step is resumable and idempotent (same Idempotency-Key resumes). Authorization is checked
again at generation time by the core (an account that changed owner fails with
`authorization_revoked`). Retry of failed members is bounded per batch. The scheduled CronJob and
the staff API call the same functions. Loans: the member list (applications open for review) is
frozen in the transaction that creates the batch.

## 8. Things deliberately not built

Document upload for loans (not required; would need private storage, type/size limits, scanning);
notifications (no SMS/e-mail is sent anywhere); interest/schedule calculation; any disbursement.
