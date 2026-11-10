# Configuration and credential matrix

Verified against the code (`core_adapter/config.py`, `nvb_statements/config.py`,
`nvb_loans/config.py`, `gateway/render_config.py`). "S" = secret (mounted file), "N" = non-secret
(ConfigMap). Paths are the ones used by the manifests. Every component validates its
configuration at start; **messages name the setting, never its value**.

Failure behaviour, in general:

* APIs: `/health/ready` returns 503 with a `problems` list ⇒ the pod never receives traffic.
  A missing/SQLite database setting fails at process start (`ConfigError`), so the pod crash-loops
  visibly instead of silently using a local file.
* Workers / relays / batch / admin commands: log `invalid configuration` + the problem list and
  exit with code 78; broker unreachable ⇒ exit 75.
* Gateway: `render_config.py` exits non-zero before nginx starts.

## Core Adapter (`ADAPTER_`)

| Variable | Req. | S/N | Mount / example | Validation / failure |
| --- | --- | --- | --- | --- |
| `ADAPTER_CORE_BASE_URL` | yes | N | `https://<core LB hostname>` | not set / not http(s) / `http://` without the dev flag ⇒ not ready |
| `ADAPTER_CORE_TOKEN_FILE` | yes | S | `/var/run/secrets/nvb/core/token` | missing/empty ⇒ not ready; calls ⇒ 503 `core_credentials_missing`. Re-read on change |
| `ADAPTER_CORE_CA_FILE` | yes for a private CA | N | `/etc/nvb/core-ca/ca.crt` | CA + hostname verification is always on; wrong CA/hostname ⇒ 503 `core_unavailable` (never bypassed) |
| `ADAPTER_CLIENTS_FILE` | yes | S | `/var/run/secrets/nvb/clients/clients.json` | missing/invalid/unknown scope ⇒ not ready, every call 401 |
| `ADAPTER_CORE_MAX_CONCURRENCY` | 4 | N | | per-replica bound; **fleet bound = replicas × value** |
| `ADAPTER_CORE_MAX_CONNECTIONS` | 4 | N | | connection pool |
| `ADAPTER_CORE_RATE_PER_SECOND` / `ADAPTER_CORE_RATE_BURST` | 20 / 20 | N | | token bucket per replica; excess ⇒ 503 `adapter_busy` (retryable) |
| `ADAPTER_CORE_QUEUE_TIMEOUT_SECONDS` | 2 | N | | wait for a slot |
| `ADAPTER_CORE_CONNECT_TIMEOUT_SECONDS` / `ADAPTER_CORE_READ_TIMEOUT_SECONDS` | 2 / 10 | N | | ⇒ 503 `core_timeout` |
| `ADAPTER_CORE_RETRY_ATTEMPTS` (+`_BASE_SECONDS`, `_MAX_SECONDS`) | 3 | N | | read-only calls, transient errors only |
| `ADAPTER_ENTRIES_MAX_PAGE_SIZE` | 1000 | N | | must be ≤ core `NVB_INTEGRATION_MAX_PAGE_SIZE` |
| `ADAPTER_ALLOW_INSECURE_HTTP` | false | N | laptop only | never set in the manifests |
| `ADAPTER_LOG_LEVEL`, `ADAPTER_INSTANCE_ID`, `ADAPTER_BUILD_REVISION` | | N | | |
| `UVICORN_SSL_CERTFILE` / `UVICORN_SSL_KEYFILE` | yes (cluster) | S | `/etc/nvb/tls/tls.crt|key` (service-serving cert) | missing ⇒ uvicorn fails to start |

## Statements (`STATEMENTS_`) — api (A), relay (R), worker (W), batch command (B), migration (M)

| Variable | Used by | Req. | S/N | Mount / example | Validation / failure |
| --- | --- | --- | --- | --- | --- |
| `STATEMENTS_DATABASE_URL` | A R W B | yes | N | `postgresql+psycopg://nvb_statements_app@host:5432/nvb_statements[?sslmode=verify-full&sslrootcert=…]` | unset ⇒ error (no SQLite fallback); SQLite refused without the dev flag and always inside Kubernetes; embedded password refused |
| `STATEMENTS_DATABASE_PASSWORD_FILE` | A R W B | yes | S | `/var/run/secrets/nvb/db/password` | missing/empty ⇒ error |
| `STATEMENTS_ADAPTER_BASE_URL` | A W B | yes | N | `https://core-adapter.<ns>.svc:8443` | `http://` refused without the dev flag |
| `STATEMENTS_ADAPTER_TOKEN_FILE` | A W B | yes | S | `/var/run/secrets/nvb/adapter/token` | missing ⇒ not ready / exit 78 |
| `STATEMENTS_ADAPTER_CA_FILE` | A W B | yes | N | `/etc/nvb/service-ca/service-ca.crt` | TLS verify always on |
| `STATEMENTS_BROKER_URL` | R W | yes | N | `amqps://host:5671/nvb` | credentials in the URL refused; `amqp://` refused unless `STATEMENTS_ALLOW_PLAINTEXT_DEPENDENCIES=true` |
| `STATEMENTS_BROKER_USERNAME_FILE` / `STATEMENTS_BROKER_PASSWORD_FILE` | R W | yes | S | `/var/run/secrets/nvb/broker/username|password` | missing ⇒ exit 78 |
| `STATEMENTS_BROKER_CA_FILE` | R W | for a private CA | N | `/etc/nvb/deps-ca/ca.crt` | |
| `STATEMENTS_BROKER_HEARTBEAT_SECONDS` | R W | 600 | N | | must exceed the longest task (the worker also services heartbeats between pages) |
| `STATEMENTS_QUEUE_NAME` / `STATEMENTS_BATCH_QUEUE_NAME` | A R W B | `nvb.statements.generate` / `nvb.statements.batch` | N | | must differ |
| `STATEMENTS_S3_ENDPOINT_URL` | A W | yes | N | `https://s3.example` | `http://` refused unless the plaintext flag is set |
| `STATEMENTS_S3_BUCKET` / `STATEMENTS_S3_REGION` | A W | `nvb-statements` / `us-east-1` | N | | bucket must exist and be private |
| `STATEMENTS_S3_ACCESS_KEY_FILE` / `STATEMENTS_S3_SECRET_KEY_FILE` | A W | yes | S | `/var/run/secrets/nvb/s3/access-key|secret-key` | missing ⇒ not ready / exit 78 |
| `STATEMENTS_S3_CA_FILE` | A W | for a private CA | N | `/etc/nvb/deps-ca/ca.crt` | |
| `STATEMENTS_STATEMENT_TIMEZONE` | A B | `Africa/Cairo` | N | | |
| `STATEMENTS_SETTLE_SECONDS` | A B | 600 | N | | must be ≥ core `NVB_INTEGRATION_SETTLE_SECONDS` |
| `STATEMENTS_MAX_MONTHS_BACK` | A B | 13 | N | | |
| `STATEMENTS_LEASE_SECONDS`, `STATEMENTS_MAX_ATTEMPTS`, `STATEMENTS_RETRY_BASE_SECONDS`, `STATEMENTS_RETRY_MAX_SECONDS` | W R | 300, 5, 30, 900 | N | | |
| `STATEMENTS_ENTRIES_PAGE_SIZE` | W | 1000 | N | | ≤ `ADAPTER_ENTRIES_MAX_PAGE_SIZE` |
| `STATEMENTS_MAX_ENTRIES_PER_STATEMENT` | W | 200000 | N | | ≤ core `NVB_INTEGRATION_MAX_STATEMENT_ENTRIES` |
| `STATEMENTS_WORKER_MAX_MESSAGES` / `STATEMENTS_WORKER_MAX_RUNTIME_SECONDS` / `STATEMENTS_IDLE_EXIT_SECONDS` | W | 50 / 900 / 30 | N | | bounds of one Job; KEDA trigger value = max messages |
| `STATEMENTS_RELAY_BATCH_SIZE`, `STATEMENTS_RELAY_POLL_SECONDS`, `STATEMENTS_STALE_REQUEUE_SECONDS`, `STATEMENTS_RELAY_HEARTBEAT_FILE` | R | 100, 1, 900, `/tmp/relay-heartbeat` | N | | heartbeat file feeds the liveness probe |
| `STATEMENTS_BATCH_MAX_ACCOUNTS`, `STATEMENTS_BATCH_DISCOVERY_PAGE_SIZE`, `STATEMENTS_BATCH_RELEASE_CHUNK`, `STATEMENTS_BATCH_MAX_RETRY_ROUNDS` | A B | 5000, 200, 200, 3 | N | | over the cap ⇒ batch `failed` (`too_many_accounts`), nothing enqueued |
| `STATEMENTS_DB_POOL_SIZE`, `STATEMENTS_DB_STATEMENT_TIMEOUT_MS`, `STATEMENTS_DOWNLOAD_MAX_BYTES`, `STATEMENTS_LOG_LEVEL` | | 5, 15000, 50 MiB, INFO | N | | |
| `STATEMENTS_ALLOW_PLAINTEXT_DEPENDENCIES` | R W A | false | N | `true` only in the disposable test overlay | |
| `STATEMENTS_ALLOW_SQLITE`, `STATEMENTS_ALLOW_INSECURE_HTTP` | | false | N | laptop/unit tests only | never in the manifests (validated) |
| `STATEMENTS_MIGRATION_DATABASE_URL` | M | yes | N | `postgresql+psycopg://nvb_statements_owner@host/nvb_statements` | unset ⇒ exit |
| `STATEMENTS_MIGRATION_PASSWORD_FILE` | M | yes | S | `/var/run/secrets/nvb/db/password` (owner Secret) | |
| `STATEMENTS_APP_ROLE` | M | recommended | N | `nvb_statements_app` | grants runtime privileges (audit tables INSERT/SELECT only) |
| `UVICORN_SSL_CERTFILE` / `UVICORN_SSL_KEYFILE` | A | yes (cluster) | S | `/etc/nvb/tls/…` | |

## Loans (`LOANS_`) — same pattern

`LOANS_DATABASE_URL`, `LOANS_DATABASE_PASSWORD_FILE`, `LOANS_ADAPTER_BASE_URL`,
`LOANS_ADAPTER_TOKEN_FILE`, `LOANS_ADAPTER_CA_FILE`, `LOANS_BROKER_URL`,
`LOANS_BROKER_USERNAME_FILE`, `LOANS_BROKER_PASSWORD_FILE`, `LOANS_BROKER_CA_FILE`,
`LOANS_BROKER_HEARTBEAT_SECONDS`, `LOANS_ALLOW_PLAINTEXT_DEPENDENCIES`,
`LOANS_MIGRATION_DATABASE_URL`, `LOANS_MIGRATION_PASSWORD_FILE`, `LOANS_APP_ROLE` behave exactly
like their `STATEMENTS_` counterparts (same mounts, same validation, same failure behaviour).
Loans has no object storage.

| Variable | Default | Notes |
| --- | --- | --- |
| `LOANS_QUEUE_NAME` | `nvb.loans.summarize` | separate from the statements queues |
| `LOANS_SUMMARY_TIMEZONE` / `LOANS_SUMMARY_MONTHS` | `Africa/Cairo` / 3 | months summarised per application |
| `LOANS_SETTLE_SECONDS` | 600 | ≥ core settle seconds |
| `LOANS_MAX_AMOUNT_MINOR` / `LOANS_MAX_TERM_MONTHS` | 500000000 / 84 | demo limits |
| `LOANS_LEASE_SECONDS`, `LOANS_MAX_ATTEMPTS`, `LOANS_RETRY_BASE_SECONDS`, `LOANS_RETRY_MAX_SECONDS` | 120, 5, 30, 900 | |
| `LOANS_WORKER_MAX_MESSAGES` / `LOANS_WORKER_MAX_RUNTIME_SECONDS` / `LOANS_IDLE_EXIT_SECONDS` | 50 / 600 / 30 | bounds of one Job |
| `LOANS_BATCH_MAX_APPLICATIONS` / `LOANS_BATCH_MAX_RETRY_ROUNDS` | 5000 / 3 | |
| `LOANS_RELAY_*`, `LOANS_STALE_REQUEUE_SECONDS`, `LOANS_RELAY_HEARTBEAT_FILE` | as statements | |

## Gateway (`GATEWAY_`)

| Variable | Req. | S/N | Example | Validation / failure |
| --- | --- | --- | --- | --- |
| `GATEWAY_PUBLIC_ORIGIN` | yes | N | `https://<Route host>` | must be `https://`; used for the Origin check; must equal the Route host |
| `GATEWAY_CORE_UPSTREAM` | yes | N | `https://<core LB hostname>` | no path; `http://` refused |
| `GATEWAY_CORE_CA_FILE` | yes | N | `/etc/nvb/core-ca/ca.crt` | must be a readable file; `proxy_ssl_verify on` with it |
| `GATEWAY_CORE_TLS_NAME` | no | N | name on the core certificate | defaults to the upstream hostname; set it when the LB is addressed by a name that differs from the certificate — **the certificate/DNS must be corrected, verification is never disabled** |
| `GATEWAY_STATEMENTS_UPSTREAM` / `GATEWAY_LOANS_UPSTREAM` | yes | N | `https://statements-api.<ns>.svc:8443` | both https |
| `GATEWAY_INTERNAL_CA_FILE` | yes | N | `/etc/nvb/service-ca/service-ca.crt` | |
| `GATEWAY_TLS_CERT_FILE` / `GATEWAY_TLS_KEY_FILE` | yes | S | `/etc/nvb/tls/tls.crt|key` | required unless the dev flag is set |
| `GATEWAY_LISTEN` | `8443` | N | | |
| `GATEWAY_LOGIN_RATE` / `GATEWAY_LOGIN_BURST` | `30r/m` / `20` | N | | per client address |
| `GATEWAY_RUN_DIR`, `GATEWAY_PORTAL_ROOT`, `GATEWAY_MIME_TYPES`, `GATEWAY_ACCESS_LOG`, `GATEWAY_ERROR_LOG`, `GATEWAY_WORKER_PROCESSES` | image defaults | N | | |
| `GATEWAY_ALLOW_INSECURE_UPSTREAMS` | false | N | laptop only | never in the manifests (validated) |

## Core settings that must agree

See `docs/CORE_PATCH.md`. Agreement rules enforced by `scripts/validate_manifests.py`:
settle (statements, loans ≥ core), page size (statements ≤ adapter ≤ core), max entries
(statements ≤ core), identical timezones, `*_WORKER_MAX_MESSAGES` = KEDA trigger value, adapter
replicas × concurrency ≤ fleet bound, https everywhere, no localhost/SQLite, no credentials in URLs.

## Internal TLS design (declared, and what the manifests implement)

| Hop | Transport | Verification |
| --- | --- | --- |
| Browser → Route | public TLS at the router (re-encrypt), HSTS, HTTP→HTTPS redirect | router certificate |
| Router → gateway pod | TLS (service-serving cert) | router trusts the service CA |
| Gateway → core LB | TLS | CA file + hostname (`proxy_ssl_verify on`, `proxy_ssl_name`) |
| Gateway → statements-api / loans-api | TLS (service-serving cert) | service CA + service hostname |
| statements / loans → core-adapter | TLS + bearer service credential | service CA + hostname |
| core-adapter → core LB | TLS + bearer client credential | CA file + hostname |
| services → PostgreSQL / RabbitMQ / S3 | PoC: TLS (`sslmode=verify-full`, `amqps`, `https`) with `deps-ca`. **Test overlay: plaintext inside the namespace** (explicit flag, NetworkPolicy-restricted, synthetic data) | |

Service-serving certificates are rotated by OpenShift; uvicorn and nginx read them at start, so
restart the Deployments after a rotation (`oc rollout restart`) — the service CA rotates
certificates well before expiry.

## Rotation

Service tokens (adapter → core, service → adapter), no downtime:

1. Generate a new token; add its SHA-256 digest as a **second** entry in the callee's
   `clients.json` (`token_sha256: [current, next]`) and update that Secret / the core's file.
   Callee picks it up within ~15 s (file mtime) — on OpenShift a Secret volume update can take up
   to ~1–2 minutes to project.
2. Update the caller's token Secret. The caller re-reads the file (adapter: on change;
   statements/loans: every call). Verify.
3. Remove the old digest from `clients.json`.

Database / broker / S3 passwords: change at the service, update the Secret, `oc rollout restart`
the Deployments (database pools hold open connections; relays reconnect on failure). Workers pick
the new files up on their next Job. Compromise: remove the digest (step 3 first) — the token stops
working within the reload interval.
