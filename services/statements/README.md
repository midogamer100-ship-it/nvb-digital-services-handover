# statements (statements-api, statements-relay, statements-worker)

One Python package and one image (`nvb-statements`), deployed as three separately scalable components:

| Component | Command | Kind | Scales on |
| --- | --- | --- | --- |
| statements-api | `uvicorn --factory nvb_statements.api:app_factory --port 8080` (default CMD) | Deployment | HTTP load |
| statements-relay | `python -m nvb_statements.relay` | Deployment, 1 replica (several are safe: `SKIP LOCKED`) | — |
| statements-worker | `python -m nvb_statements.worker --drain` | Job / KEDA ScaledJob | queue depth |
| migration | `alembic -c migrations/statements/alembic.ini upgrade head` | one-off Job | — |

Nothing runs migrations, seeding or resets on startup.

## API (`/x/statements/v1`)

| Method | Path | Auth | Result |
| --- | --- | --- | --- |
| POST | `/requests` `{account_id, period:"YYYY-MM"}` + `Idempotency-Key` | session + CSRF | `202` + `Location`/`status_url` |
| GET | `/requests` | session | caller's requests (accounts still owned) |
| GET | `/requests/{id}` | session | status |
| POST | `/requests/{id}/cancel` | session + CSRF | queued → cancelled |
| GET | `/requests/{id}/document` | session | PDF stream after ownership + checksum check |

Identity: the core `nvb_session` cookie (same origin through the gateway) is verified through the Core
Adapter on **every** request; CSRF is verified by the core for the same session. Client identity headers
are ignored. Status/download always re-check current ownership.

State model and reliability rules: see the docstrings in `nvb_statements/models.py`, `worker.py`, `relay.py`.

### Background-task authorization

The request row records actor (`requested_by_subject`), owner, account, purpose, period/timezone and
`data_scope=statement:single_account`. No cookie or password is stored or queued. The worker uses its
own `statements:read` service credential; the core re-checks that the owner subject is active and still
owns the account at extraction time.

| Event after request | Behaviour |
| --- | --- |
| Logout / session expiry | Generation continues (it was authorised by a live session); download needs a new session. |
| User suspended / account no longer owned | Worker gets 404 → `failed: authorization_revoked`; download → 404. |
| Customer cancels while queued | `cancelled`; queued messages are acked without work. |

## Environment variables (`STATEMENTS_` prefix)

| Variable | Default | Used by |
| --- | --- | --- |
| `DATABASE_URL` | dev SQLite | all — URL **without** password |
| `DATABASE_PASSWORD_FILE` | — | all |
| `DB_POOL_SIZE`, `DB_STATEMENT_TIMEOUT_MS` | 5, 15000 | all |
| `ADAPTER_BASE_URL`, `ADAPTER_TOKEN_FILE`, `ADAPTER_CA_FILE`, `ADAPTER_TIMEOUT_SECONDS` | `http://core-adapter:8080`, —, —, 15 | api, worker |
| `BROKER_URL` (`amqps://host:5671/vhost`), `BROKER_USERNAME_FILE`, `BROKER_PASSWORD_FILE`, `BROKER_CA_FILE` | — | relay, worker |
| `QUEUE_NAME` | `nvb.statements.generate` | relay, worker |
| `S3_ENDPOINT_URL`, `S3_BUCKET`, `S3_REGION`, `S3_ACCESS_KEY_FILE`, `S3_SECRET_KEY_FILE`, `S3_CA_FILE` | —, `nvb-statements`, `us-east-1` | api (download), worker |
| `STATEMENT_TIMEZONE` | `Africa/Cairo` | api |
| `SETTLE_SECONDS` | 600 (must be ≥ core `NVB_INTEGRATION_SETTLE_SECONDS`) | api |
| `MAX_MONTHS_BACK` | 13 | api |
| `LEASE_SECONDS`, `MAX_ATTEMPTS`, `RETRY_BASE_SECONDS`, `RETRY_MAX_SECONDS` | 300, 5, 30, 900 | worker |
| `ENTRIES_PAGE_SIZE`, `MAX_ENTRIES_PER_STATEMENT` | 1000, 200000 | worker |
| `IDLE_EXIT_SECONDS` | 30 | worker `--drain` |
| `RELAY_BATCH_SIZE`, `RELAY_POLL_SECONDS` | 100, 1 | relay |
| `LOG_LEVEL`, `BUILD_REVISION` | INFO, from image | all |

## Ports / dependencies

| Component | Listens | Needs |
| --- | --- | --- |
| api | 8080 | statements DB (readiness), core-adapter, object storage (download only) |
| relay | — | statements DB, RabbitMQ |
| worker | — | statements DB, RabbitMQ, core-adapter, object storage |

Probes (api): `/health/live` process only; `/health/ready` = DB reachable. Adapter/storage outages return
explicit 503s on the affected calls instead of failing readiness. Metrics: `/metrics` (api); worker/relay
expose counters in-process (scrape path to be agreed for Jobs — likely Pushgateway is *not* used; logs +
DB/queue metrics instead).

## Tests

```bash
../../.venv/Scripts/python -m pytest      # 56 tests: SQLite + in-memory broker/store + fake adapter
```

Real RabbitMQ / S3 / PostgreSQL tests wait for the infra-provided disposable environment
(`docs/INFRA_REQUEST_TEST_ENVIRONMENT.md`).
