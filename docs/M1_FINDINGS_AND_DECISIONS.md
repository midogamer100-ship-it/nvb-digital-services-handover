# M1 — Source findings, decisions and open items

## Source revision

* Implemented against `NVB-Monolith` commit `ffd32015aa00564054bb177f6580196ce1b1355c` (`main`, clean tree).
* `D:\Projects\NVB-Monolith.zip` was compared file-by-file (SHA-256) with the checkout: identical,
  including `.git` (HEAD = `ffd3201`). `node_modules`/`frontend/dist` were not compared.
* **The deployed VM revision is unknown.** No live compatibility is claimed.
* A sibling repository `D:\Projects\NVB-Microservices` exists (a separate decomposition of the core into
  auth/customer/accounts/ops services with HMAC gateway identity). It is **not** used here; which repo the
  VMs pull from must be confirmed by infra.

## Re-verified facts (file evidence)

| Fact | Evidence | Status |
| --- | --- | --- |
| Python 3.12 / FastAPI / SQLAlchemy / Alembic / PostgreSQL | `backend/pyproject.toml`, `requirements.txt`, `alembic/` | ✔ |
| API + SPA in one app, docs/openapi disabled | `app/main.py` (`docs_url=None, redoc_url=None, openapi_url=None`) | ✔ |
| Accounts / transactions / export / profile / auth routes | `app/api/*.py` | ✔ |
| `page_size ≤ 100`, offset pagination, `posted_at DESC, id DESC`, `MAX_EXPORT_ROWS=10_000` | `app/api/transactions.py` | ✔ |
| UTC day filters `[date_from 00:00Z, date_to+1 00:00Z)` | `transactions.py::_scoped_query` | ✔ |
| Opaque `nvb_session` HttpOnly cookie, SameSite=Lax, path `/`, no Domain, Secure configurable | `app/api/auth.py::login` | ✔ |
| DB sessions, idle 15 / absolute 480 min, sliding idle expiry | `app/security.py`, `app/config.py` | ✔ |
| CSRF: per-session token, header `X-CSRF-Token`, all non-GET/HEAD/OPTIONS | `app/deps.py::get_auth` | ✔ |
| `/api/auth/me` returns the CSRF token and no stable subject id | `auth.py::session_payload` | ✔ (note: anyone holding the cookie can read the CSRF token) |
| Roles `customer/admin/demo` with DB CHECK | `models.py` `ck_users_role` | ✔ |
| admin does not bypass `require_customer`; `owned_account` → 404 | `deps.py`, `accounts.py` | ✔ |
| Money as BIGINT minor units, masked numbers | `models.py`, `money.py`, `serializers.py` | ✔ |
| Demo CPU workload is in-process, not PDF/batch | `api/demo.py`, `services/workload.py` | ✔ |
| `OutboxMessage` is a demo mailbox | `models.py` docstring | ✔ |
| No loans, no PDF, no queue, no OTP/SMS, no git-pull automation | repo-wide search | ✔ (`docs/VM_DEPLOYMENT.md` only mentions a manual `git clone`) |

### Additional findings that affect design

1. **`posted_at` is taken before the row lock** (`services/transfers.py::_execute_locked`,
   `services/cash.py::_execute_locked`: `now = utcnow()` then `SELECT … FOR UPDATE`). A row can commit with
   an older `posted_at` than rows already visible, and per-account `id` order can differ from `posted_at`
   order by milliseconds. ⇒ settle window + manifest verification in the dataset contract.
2. **Every balance change writes a ledger entry and accounts start at 0** (seed `_history`, `onboarding`,
   cash, transfers). ⇒ historical opening balance = Σ signed entries before the period, reconcilable
   against the current balance in the same snapshot.
3. Seeded history includes entries on the anchor day at random 07:00–21:59 UTC, which can be **in the future**
   relative to "now" on the seeding day.
4. `csrf_token` is stored in plaintext in `user_sessions` (session token is hashed). Not changed.
5. `users.id` is reused after `reset-demo-data` (`TRUNCATE … RESTART IDENTITY`). ⇒ subject includes `created_at`.
6. Nginx on the VM forwards everything except `/metrics` (`deploy/vm/nginx-nvb.conf`), so `/internal/v1`
   would be reachable through the existing route once enabled. Bearer auth is mandatory; infra may additionally
   block `/internal/` at nginx for external clients.

## ⚠ Destructive test fixture (confirmed)

`backend/tests/conftest.py`: when `NVB_TEST_DATABASE_URL` is a PostgreSQL URL the `app` fixture runs
`DROP SCHEMA public CASCADE; CREATE SCHEMA public` and then seeds. Pointing it at the core/shared database
destroys it. `backend/app/cli.py reset-demo-data` TRUNCATEs core tables (guarded only by
`NVB_ALLOW_DEMO_RESET`). Neither was run against any PostgreSQL. Use `scripts/safe_core_tests.py`, which strips
inherited `NVB_*`, defaults to SQLite and refuses PostgreSQL unless the host is allow-listed (localhost by
default), the DB name starts with `nvb_disposable_test_` and `--i-confirm-disposable` is passed.

## Decisions (proposed defaults — confirm with infra)

| # | Decision | Default taken | Blocking? |
| --- | --- | --- | --- |
| D1 | Additive core patch allowed? | Prepared, disabled, not deployed | Blocks M2 live, not local |
| D2 | Service → core auth | Scoped bearer client credentials, SHA-256 digests in a mounted Secret, rotation by file; mTLS on top if infra provides certs | Needs infra OK |
| D3 | Browser origin | New HTTP-aware gateway hostname (e.g. `nvb-digital-…apps…`), TLS edge/re-encrypt, proxies `/api/auth/*` to core, extension APIs under `/x/statements`, `/x/loans`. Existing passthrough route untouched | Hostname needed for M2 portal |
| D4 | Broker | RabbitMQ (quorum queues, publisher confirms, manual ack, DLX) | Needs infra OK |
| D5 | Object storage | S3-compatible private bucket (ODF/NooBaa or MinIO locally) | Needs infra OK |
| D6 | Extension DB | One PostgreSQL instance, DBs `nvb_statements`, `nvb_loans`, separate owner/app roles; migrations as one-off Jobs | Needs infra OK |
| D7 | Workers | KEDA ScaledJob on RabbitMQ queue length, finite `--drain` worker; fallback: Deployment + graceful drain | Depends on installed KEDA |
| D8 | Statement period | Calendar month in `Africa/Cairo` converted to UTC instants; boundaries printed on the PDF | Business confirm |
| D9 | Local disposable infra | **No container runtime found on this workstation (no docker/podman/psql).** | **Blocks PostgreSQL/RabbitMQ/S3 tests** |

## Not verified

Live cluster, deployed core revision, DNS/Git-pull VM changes, HAProxy/Route state, installed operators,
PostgreSQL behaviour of the patch (REPEATABLE READ snapshot, locking), performance numbers.
