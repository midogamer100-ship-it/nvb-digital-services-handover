# Core integration API v1 (`/internal/v1`) — additive core patch

Status: **implemented in the core patch (`docs/CORE_PATCH.md`), locally integration-tested on
SQLite and disposable PostgreSQL, not deployed to any core VM, disabled by default**
(`NVB_INTEGRATION_ENABLED=false`). Path version `v1`; statement dataset version `statement-v2`.

## Authentication

* `Authorization: Bearer <client token>` on every request (RFC 6750 bearer over TLS).
* The core stores only SHA-256 digests in `NVB_INTEGRATION_CLIENTS_FILE`; several digests per
  client allow overlap during rotation; the file is re-read on change (≤ 15 s).
* Missing/invalid file ⇒ every call 401. Unknown scope in the file ⇒ file rejected ⇒ 401.
* Tokens shorter than 32 or longer than 512 characters are rejected without a lookup.
* Kubernetes ServiceAccount tokens and browser sessions are not accepted here.

## Scopes and endpoints

| Endpoint | Scope | Notes |
| --- | --- | --- |
| `POST /internal/v1/sessions/introspect` `{session_token, csrf_token?, include_accounts?}` | `identity:introspect` (+`accounts:read`) | Uses the core's `load_session` (same expiry, logout, suspension semantics). Returns `{"active": false}` or subject / username / role / customer_number / `csrf_valid`. Never returns the CSRF secret. |
| `GET /internal/v1/subjects/{subject}/accounts` | `accounts:read` | Active customer subjects only. |
| `GET /internal/v1/accounts/{id}/statement-manifest?subject&period_start&period_end` | `statements:extract` or `summaries:read` | One REPEATABLE READ, READ ONLY snapshot on PostgreSQL. Audited. |
| `GET /internal/v1/accounts/{id}/statement-entries?subject&period_start&period_end&cursor&limit` | `statements:extract` | Keyset pagination, `limit` ≤ `NVB_INTEGRATION_MAX_PAGE_SIZE`. |
| `GET /internal/v1/discovery/statement-accounts?after_account_id&limit&include_load_test` | `accounts:discover` | Active accounts of active customers. Audited. |

Errors: `{"error": {"code", "message"}}`. Codes: `invalid_client` 401, `insufficient_scope` 403,
`subject_not_found` / `account_not_found` / `not_found` 404 (no existence leak),
`period_not_settled` 409, `invalid_period` / `period_too_long` / `naive_datetime` /
`invalid_cursor` / `invalid_subject` / `dataset_too_large` 422, `export_capacity` 429
(+`Retry-After`), `ledger_reconciliation_failed` / `snapshot_inconsistent` 500.

## Subject

`nvb-user:<users.id>:<users.created_at unix seconds>`. Immutable for a user; rejected when the
row no longer matches (detects most id reuse after a demo reset). A core demo reset must be
paired with an extension data reset.

## Statement dataset contract `statement-v2`

* Period: UTC instants, half-open `[period_start, period_end)`. Local periods (a Cairo calendar
  month) are converted by the caller; the core never reinterprets dates.
* Ordering: `posted_at ASC, id ASC` (deterministic for tied timestamps).
* `opening_balance_minor` = Σ signed amounts with `posted_at < period_start`;
  `closing_balance_minor` = opening + credits − debits in the period. Never the current balance.
* Reconciliation in the same snapshot: Σ all signed entries must equal `accounts.balance_minor`,
  else `500 ledger_reconciliation_failed`.
* Entry fields: `entry_id, posted_at, posted_at_us, reference, direction, type, category,
  amount_minor, signed_amount_minor, balance_after_minor, description, counterparty`.
  `posted_at_us` = integer microseconds since the Unix epoch.

### What "consistent" means here (and what it does not)

There is **no immutable snapshot** kept by the core and no transaction is held between requests.
The guarantee is produced by verification:

1. The manifest is computed in ONE database snapshot and carries
   `entries_sha256` = SHA-256 over the canonical encoding of **every** entry of the period in
   dataset order. Canonical line per entry (UTF-8, `\n`-terminated, compact JSON array,
   non-ASCII not escaped):
   `[entry_id, posted_at_us, direction, amount_minor, balance_after_minor, reference, type, category, description, counterparty]`
2. `dataset_id` = SHA-256 over version, account, period, balances, totals, count, first/last id
   **and `entries_sha256`**.
3. The caller pages the entries in separate short requests, recomputes the digest over what it
   received and requires equality with the manifest. Equality proves the paged set is exactly the
   set of the manifest's snapshot: an insert (including a back-dated one), an in-place edit or a
   delete that became visible between the manifest and any page changes the digest — even when
   count and totals stay identical.
4. The caller re-reads the manifest and requires the same `dataset_id`.
5. Any mismatch ⇒ retry later (bounded). A partial or drifting dataset is never rendered.

The result is "the period as of one core snapshot at `generated_at`". A later change of the
period (the ledger is append-only by application convention, not by a database constraint)
produces a different `dataset_id` and therefore a different document on the next generation; it
does not alter an already generated one.

Ledger facts this relies on / tolerates (verified in the core source and by tests):

| Case | Behaviour |
| --- | --- |
| Concurrent commits | Postings take `posted_at = now()` before the row lock, so a row can commit with an older timestamp. Not assumed away: detected by steps 3–4. PostgreSQL tests: manifest transaction is `repeatable read` + `read only`; a commit made mid-manifest is invisible to it. |
| Back-dated entries | Possible (seed data; in-flight postings). Detected by the digest / `dataset_id`; a back-dated row before the period changes `opening_balance_minor` ⇒ different `dataset_id`. |
| Corrections / reversals | The core has no update/delete path for ledger rows (transfers, cash, onboarding, seed only INSERT). If one happened anyway (manual SQL), the digest changes ⇒ dataset rejected / new document. |
| Timestamp ties | Order `(posted_at, id)`; keyset cursor on both; ties are hashed in id order. |
| Empty periods | `entry_count 0`, `entries_sha256` = SHA-256 of the empty string, opening = closing. |
| Account opened during / after the period | `account_opened_in_period` / `account_opened_after_period`; opening balance 0. |
| More than 10,000 records | No row cap on entries (tested with 10,050); the manifest hashes at most `NVB_INTEGRATION_MAX_STATEMENT_ENTRIES` and returns `dataset_too_large` above it — never a truncated dataset. |
| Opening / closing balances | Historical sums, cross-checked by the caller: `opening + Σ signed = closing`. |
| UTC vs Africa/Cairo | The core only sees UTC instants; the statements service converts the local month (DST-aware) and prints both on the PDF. |

* Settlement: `period_end ≤ now − NVB_INTEGRATION_SETTLE_SECONDS` (default 300). This only makes
  verification failures rare; it is not the consistency mechanism.
* Core protection: per-process bound `NVB_INTEGRATION_MAX_CONCURRENT_EXPORTS`, 429 beyond it; no
  transaction between pages; expensive work (PDF, batch) happens outside the core.

## Settings added to the core

See `docs/CORE_PATCH.md`. No schema change, no migration.
