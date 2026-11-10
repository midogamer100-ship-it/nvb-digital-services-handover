# core-adapter

The only extension component that talks to the NVB core. It exposes the stable
`core-adapter-v1` contract to extension services and maps it to the core's
`/internal/v1` integration API (see `../../contracts/core-integration-v1.md`).

It never reads core tables, never holds core database credentials and never posts
financial data.

## Run locally

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt   # Windows path; use .venv/bin on Linux
.venv/Scripts/python -m pytest
ADAPTER_CORE_BASE_URL=https://core.example.invalid ADAPTER_CORE_TOKEN_FILE=... ADAPTER_CLIENTS_FILE=... \
  uvicorn --factory core_adapter.app:app_factory --port 8080
```

## Endpoints

| Method | Path | Scope | Purpose |
| --- | --- | --- | --- |
| POST | `/v1/identity/resolve` | `identity:resolve` (+`accounts:read` for `include_accounts`) | Session introspection, stable subject, CSRF check |
| GET | `/v1/subjects/{subject}/accounts` | `accounts:read` | Accounts of an active customer subject |
| GET | `/v1/statement-datasets/{account_id}/manifest` | `statements:read` or `summaries:read` | Balances/totals/count + `dataset_id` |
| GET | `/v1/statement-datasets/{account_id}/entries` | `statements:read` | Keyset-paginated entries |
| GET | `/v1/discovery/statement-accounts` | `discovery:read` | Batch account discovery |
| GET | `/health/live`, `/health/ready`, `/metrics` | none | Probes and Prometheus metrics |

Errors are always `{"error": {"code", "message", "retryable", "request_id"}}`.

## Environment variables

| Variable | Default | Notes |
| --- | --- | --- |
| `ADAPTER_CORE_BASE_URL` | — (required) | Infra-agreed core LB URL. Not hard-coded. |
| `ADAPTER_CORE_TOKEN_FILE` | — (required) | Mounted Secret with the adapter's core client token. Re-read on change. |
| `ADAPTER_CORE_CA_FILE` | system trust | PEM bundle for the core certificate. Hostname verification is always on. |
| `ADAPTER_ALLOW_INSECURE_HTTP` | `false` | Local development only. TLS verification can never be disabled. |
| `ADAPTER_CLIENTS_FILE` | — (required) | Mounted Secret: inbound clients (SHA-256 digests + scopes). |
| `ADAPTER_CORE_CONNECT_TIMEOUT_SECONDS` | `2` | |
| `ADAPTER_CORE_READ_TIMEOUT_SECONDS` | `10` | |
| `ADAPTER_CORE_MAX_CONNECTIONS` | `4` | Connection pool size per replica. |
| `ADAPTER_CORE_MAX_CONCURRENCY` | `4` | In-flight core calls per replica. Fleet bound = replicas × this. |
| `ADAPTER_CORE_QUEUE_TIMEOUT_SECONDS` | `2` | Wait for a slot before returning 503 `adapter_busy`. |
| `ADAPTER_CORE_RATE_PER_SECOND` / `_RATE_BURST` | `20` / `20` | Token bucket per replica. |
| `ADAPTER_CORE_RETRY_ATTEMPTS` | `3` | Read-only calls only; transient failures only. |
| `ADAPTER_CORE_RETRY_BASE_SECONDS` / `_MAX_SECONDS` | `0.2` / `2` | Capped exponential backoff with jitter. |
| `ADAPTER_ENTRIES_MAX_PAGE_SIZE` | `1000` | Must be ≤ the core's `NVB_INTEGRATION_MAX_PAGE_SIZE`. |
| `ADAPTER_LOG_LEVEL` | `INFO` | JSON logs to stdout. |
| `ADAPTER_BUILD_REVISION` | from image | Shown by readiness. |

## Ports and dependencies

| Port | Protocol | Inbound from | Outbound to |
| --- | --- | --- | --- |
| 8080 | HTTP (in-cluster; TLS/mTLS per infra) | gateway, statements-api, statements-worker, loans-* | core LB (HTTPS) |

## Probes

* Liveness `/health/live`: process only. Never depends on the core.
* Readiness `/health/ready`: configuration complete and credential files loadable.
  A core outage does **not** make the adapter unready; callers get explicit retryable
  `503 core_unavailable` instead. Restarting the adapter would not fix a core outage.

## Resources

Not measured yet. Initial *placeholder* request 50m CPU / 96Mi, limit 500m / 256Mi,
to be replaced with measurements from the local load test (M3).
