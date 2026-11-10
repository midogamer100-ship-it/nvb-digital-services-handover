# Core integration API v1 (`/internal/v1`) — proposed additive core patch

Status: **prepared locally, not deployed, disabled by default** (`NVB_INTEGRATION_ENABLED=false`).
Implemented in the isolated core clone `NVB-Monolith-integration`, branch
`feature/extension-integration-api` (based on core `ffd32015aa00564054bb177f6580196ce1b1355c`).

## Authentication

* `Authorization: Bearer <client token>` on every request (RFC 6750 bearer credential over TLS).
* The core stores only SHA-256 digests in `NVB_INTEGRATION_CLIENTS_FILE` (mounted Secret):

  ```json
  {"clients": [{"client_id": "nvb-core-adapter",
                "token_sha256": ["<current>", "<next during rotation>"],
                "scopes": ["identity:introspect", "accounts:read", "statements:extract",
                           "summaries:read", "accounts:discover"]}]}
  ```
* File re-read on change (≤15 s): rotation without a core restart. Missing/invalid file ⇒ every call 401.
* Recommended by infra decision: add mTLS (or a network policy + passthrough restriction) in front;
  the bearer check stays regardless of network placement.
* Kubernetes ServiceAccount tokens are **not** accepted.

## Scopes and endpoints

| Endpoint | Scope | Notes |
| --- | --- | --- |
| `POST /internal/v1/sessions/introspect` `{session_token, csrf_token?, include_accounts?}` | `identity:introspect` (+`accounts:read`) | Uses the core's own `load_session` (same expiry, logout, suspension semantics). Returns `{"active": false}` or subject/role/customer_number/`csrf_valid`. Never returns the CSRF secret. |
| `GET /internal/v1/subjects/{subject}/accounts` | `accounts:read` | Active customer subjects only. |
| `GET /internal/v1/accounts/{id}/statement-manifest?subject&period_start&period_end` | `statements:extract` or `summaries:read` | One REPEATABLE READ READ ONLY snapshot (PostgreSQL). Audited. |
| `GET /internal/v1/accounts/{id}/statement-entries?subject&period_start&period_end&cursor&limit` | `statements:extract` | Keyset pagination, `limit` ≤ `NVB_INTEGRATION_MAX_PAGE_SIZE` (1000). |
| `GET /internal/v1/discovery/statement-accounts?after_account_id&limit&include_load_test` | `accounts:discover` | Active accounts of active customers. Audited. |

Errors: `{"error": {"code", "message"}}`. Codes: `invalid_client` 401, `insufficient_scope` 403,
`subject_not_found`/`account_not_found` 404 (no existence leak), `period_not_settled` 409,
`invalid_period`/`period_too_long`/`naive_datetime`/`invalid_cursor`/`invalid_subject` 422,
`export_capacity` 429 (+`Retry-After`), `ledger_reconciliation_failed` 500.

## Subject

`nvb-user:<users.id>:<users.created_at unix seconds>`. Stable and immutable for a user;
rejected if the row no longer matches (detects most id reuse after a demo reset). A core
demo reset must still be paired with an extension data reset (runbook).

## Statement dataset contract `statement-v1`

* Period: UTC instants, half-open `[period_start, period_end)` — identical to the existing
  `/api/transactions` UTC-day filters. Local periods (e.g. a Cairo calendar month) are converted
  by the caller; the core never reinterprets dates.
* Ordering: `posted_at ASC, id ASC` (deterministic for tied timestamps).
* `opening_balance_minor` = Σ signed amounts with `posted_at < period_start`;
  `closing_balance_minor` = opening + credits − debits in the period. Never the current balance.
* Reconciliation in the same snapshot: Σ all signed entries must equal `accounts.balance_minor`,
  else `500 ledger_reconciliation_failed` (verified true for seed, sign-up, cash and transfers:
  accounts start at 0 and every balance change writes a ledger entry).
* `dataset_id` = SHA-256 over version, account, period, balances, totals, count, first/last id.
* Settlement: `period_end ≤ now − NVB_INTEGRATION_SETTLE_SECONDS` (default 300). Reason: postings
  take `posted_at = now()` **before** waiting for the account row lock, so a row can commit with a
  slightly older `posted_at`. A max id/timestamp is therefore not a completeness proof.
* Caller verification (mandatory): page all entries, check count and credit/debit totals equal the
  manifest, check `opening + Σ signed = closing`, re-fetch the manifest and require the same
  `dataset_id`. Any mismatch ⇒ retry later (bounded) — never render a partial statement.
* No row cap (the 10,000-row CSV export limit does not apply); per-process concurrency bound
  `NVB_INTEGRATION_MAX_CONCURRENT_EXPORTS` (2) protects the core; no transaction is held between pages.

## Settings added to the core (`NVB_` prefix)

`INTEGRATION_ENABLED` (false), `INTEGRATION_CLIENTS_FILE`, `INTEGRATION_MAX_CONCURRENT_EXPORTS` (2),
`INTEGRATION_EXPORT_QUEUE_TIMEOUT_SECONDS` (2), `INTEGRATION_MAX_PAGE_SIZE` (1000),
`INTEGRATION_MAX_PERIOD_DAYS` (400), `INTEGRATION_SETTLE_SECONDS` (300),
`INTEGRATION_DISCOVERY_MAX_PAGE_SIZE` (500).

No schema change. No migration. Existing routes, `require_customer`, `owned_account`,
money handling and idempotency are untouched.
