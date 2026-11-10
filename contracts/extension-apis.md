# Extension API, adapter and queue contracts

All browser-facing APIs are reached on the gateway origin, authenticated by the core session cookie
`nvb_session` (verified through the Core Adapter on every request). Unsafe methods need
`X-CSRF-Token` (the token returned by the core's login / `GET /api/auth/me`). Errors:
`{"error": {"code", "message", "request_id"}}`. Money is integer minor units everywhere.

Common status codes: 401 `authentication_required`, 403 `csrf_invalid` / `customer_only` /
`forbidden`, 404 `not_found` (also for other people's objects), 409 state conflicts, 422
validation, 503 `identity_unavailable` / `storage_unavailable`.

## Gateway origin (browser)

| Path | Upstream |
| --- | --- |
| `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me` | core (unchanged core API) |
| `/x/statements/v1/...` | statements-api |
| `/x/loans/v1/...` | loans-api |
| any other `/api/*`, `/x/*`, `/internal/*`, `/health/*`, `/metrics` | JSON 404 at the gateway |
| everything else (GET) | portal static files, fallback `/index.html` |

## statements-api — prefix `/x/statements/v1`

| Method + path | Who | Notes |
| --- | --- | --- |
| `GET /me` | any session | `{subject, username, display_name, core_role, extension_roles}` |
| `GET /accounts` | customer | own accounts (masked numbers) + `statement_timezone` |
| `POST /requests` `{account_id, period: "YYYY-MM"}` + `Idempotency-Key` (8–64 chars) | customer | 202 + `Location`; replay returns the same request with `Idempotent-Replayed: true`; same key, different body ⇒ 409 `idempotency_conflict`; open/too-old month ⇒ 422 |
| `GET /requests?limit=` | customer | own requests (including batch-generated ones for own accounts) |
| `GET /requests/{id}` | owner | status `queued | running | completed | failed | cancelled` |
| `POST /requests/{id}/cancel` | owner | only while `queued` (409 otherwise) |
| `GET /requests/{id}/document` | owner | PDF stream; ownership of the account is re-checked; SHA-256 of the stored object is verified; `Cache-Control: no-store` |
| `POST /batches` `{period, include_load_test?}` + `Idempotency-Key` | `batch_operator` | 202; creates/resumes a monthly-statement batch |
| `GET /batches`, `GET /batches/{id}` | `batch_operator` | status + progress counts + failures (`request_id, account_masked, error_code, attempts`) |
| `POST /batches/{id}/resume` | `batch_operator` | continue an interrupted freeze/release |
| `POST /batches/{id}/retry-failed` | `batch_operator` | re-queue failed members; bounded by `STATEMENTS_BATCH_MAX_RETRY_ROUNDS` |
| `POST /batches/{id}/cancel` | `batch_operator` | stop unreleased/queued members; results are kept |
| `GET /admin/roles`, `POST /admin/roles/grant`, `POST /admin/roles/revoke` `{subject, role, reason?, label?}` | `extension_admin` | roles of this service: `batch_operator`, `extension_admin` |

Batch status: `freezing → releasing → running → completed | completed_with_failures`; also
`cancelled`, `failed` (`too_many_accounts`). Progress: `{queued, running, completed, failed,
cancelled, total, pending_release}`.

## loans-api — prefix `/x/loans/v1`

| Method + path | Who | Notes |
| --- | --- | --- |
| `GET /me` | any session | identity + roles of this service |
| `POST /applications` `{account_id, amount_minor, term_months, monthly_income_minor, purpose?}` | customer | 201 draft (`version` 1). Floats/strings for money are rejected (strict integers) |
| `GET /applications`, `GET /applications/{id}` | owner | never exposes reviewer identity or the summary |
| `PUT /applications/{id}` `{…, version}` | owner | draft only |
| `POST /applications/{id}/submit` `{version}` | owner | `draft → submitted`; enqueues a background summary |
| `GET /review/applications[?status=]`, `GET /review/applications/{id}` | `reviewer` (assigned only), `approver`, `extension_admin` | includes summary + event history |
| `GET /review/reviewers` | `approver`, `extension_admin` | subjects holding `reviewer` |
| `POST /review/applications/{id}/assign` `{reviewer_subject, version}` | `approver`, `extension_admin` | `submitted → under_review`; reviewer must hold the role and must not be the applicant |
| `POST /review/applications/{id}/recommendation` `{recommendation: approve|reject, reason (≥10), version}` | assigned `reviewer` | |
| `POST /review/applications/{id}/decision` `{decision: approved|rejected, reason (≥10), version}` | `approver` ≠ reviewer ≠ applicant | requires a recommendation; `under_review → approved|rejected` |
| `POST /batches` + `Idempotency-Key`, `GET /batches`, `GET /batches/{id}`, `POST /batches/{id}/retry-failed` | `batch_operator` | summary refresh for applications open for review |
| `GET /admin/roles`, `POST /admin/roles/grant|revoke` | `extension_admin` | roles: `reviewer`, `approver`, `batch_operator`, `extension_admin` |

Every state change is a conditional update on `version`; a stale version ⇒ 409 `version_conflict`,
a wrong source state ⇒ 409 `invalid_state`. `approved` is a workflow state only: the response
carries `"disbursement": "none - workflow decision only; no money is posted by this system"`.

## Core Adapter (`core-adapter-v1`, in-cluster only, bearer service credentials)

| Endpoint | Scope |
| --- | --- |
| `POST /v1/identity/resolve` | `identity:resolve` (+`accounts:read` for `include_accounts`) |
| `GET /v1/subjects/{subject}/accounts` | `accounts:read` |
| `GET /v1/statement-datasets/{account_id}/manifest` | `statements:read` or `summaries:read` |
| `GET /v1/statement-datasets/{account_id}/entries` | `statements:read` |
| `GET /v1/discovery/statement-accounts` | `discovery:read` |
| `GET /health/live`, `/health/ready`, `/metrics` | none (pod network only) |

Core responses are validated against `core_adapter/contract.py`; a violation is
`502 core_contract_violation`. Errors carry `retryable`. Not exposed through the gateway.

## Queues (RabbitMQ)

| Queue | Publisher | Consumer | Message body |
| --- | --- | --- | --- |
| `nvb.statements.generate` | statements-relay | statements-worker (polled first) | `{"type": "statement.generate", "request_id": "<uuid>", "attempt": <int>}` |
| `nvb.statements.batch` | statements-relay | statements-worker (when the interactive queue is empty) | same |
| `nvb.loans.summarize` | loans-relay | loans-worker | `{"type": "loan.summarize", "task_id": "<uuid>"}` |
| `<queue>.dead` | broker (DLX `nvb.dlx`, routing key = queue) | operators | dead-lettered messages |

* Messages carry identifiers only — no tokens, no customer data. `message_id` = outbox message id.
* Queues are durable **quorum** queues with `x-dead-letter-exchange=nvb.dlx`,
  `x-dead-letter-routing-key=<queue>`, `x-delivery-limit=20`; declared by the relays on connect.
* Publishing: persistent, publisher confirms, `mandatory` (unroutable ⇒ error, outbox row kept).
* Consuming: `basic.get` with manual ack. Ack only after the database commit. Unexpected failure ⇒
  `reject(requeue=true)` + non-zero exit. Malformed message ⇒ `reject(requeue=false)` (dead letter).
* Delivery is at-least-once; handlers are idempotent (conditional claim with lease, deterministic
  object key `statements/<request_id>/<dataset_id>.pdf`, terminal states are acked without work).
* Broker user permissions (vhost `nvb`): configure/read on the service's own queues, **write on
  its queues, `nvb.dlx` and `amq.default`** (the services publish through the default exchange).
