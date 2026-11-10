# Implementation plan (file level)

Legend: ✅ done in this increment · ⏳ next · ☐ later

## M1 — boundaries and contracts

* ✅ `contracts/core-integration-v1.md` — core `/internal/v1` contract.
* ✅ Core patch (isolated clone `NVB-Monolith-integration`, branch `feature/extension-integration-api`, uncommitted):
  `backend/app/config.py` (settings, disabled), `backend/app/main.py` (conditional router),
  `backend/app/api/integration.py`, `backend/app/services/integration_exports.py`,
  `backend/tests/integration_support.py`, `backend/tests/test_integration_auth.py`,
  `backend/tests/test_integration_exports.py`.
* ✅ `services/core-adapter/` — FastAPI adapter, Containerfile, pinned deps, 30 unit tests.
* ✅ `tests/contract/test_adapter_against_core.py` — adapter ↔ patched core in-process.
* ✅ `scripts/safe_core_tests.py` — guarded core test runner.
* ⏳ Disposable PostgreSQL run of the core suite + patch (needs D9).

## M2 — one statement end to end

Status: backend chain ✅ (services/statements, migrations/statements, tests/e2e). Remaining ⏳: gateway,
portal pages, runs against real PostgreSQL/RabbitMQ/S3 (infra test environment), OpenShift manifests for review.
Implementation note: shared code lives inside the statements domain package (`nvb_statements`), not a
cross-domain `libs/` package. Actual files: `services/statements/nvb_statements/{api,service,models,period,
adapter_client,dataset,render,storage,broker,relay,worker}.py`. The list below is the original plan.

* `libs/nvb_platform/` — shared: service auth client, JSON logging, outbox, RabbitMQ publisher (confirms), S3 client.
* `services/statements-api/statements_api/`: `app.py` (POST `/x/statements` → 202 + status URL; GET list/status;
  GET `/{id}/download` → authorised stream or short-lived presigned URL), `models.py`
  (`statement_requests`, `statement_audit`, `outbox`), `authz.py` (identity via adapter on every call, CSRF on POST),
  `idempotency.py` (key + request fingerprint, 409 on mismatch).
* `migrations/statements/` — Alembic env + `0001_initial.py`; run by a one-off Job only.
* `services/statements-worker/statements_worker/`: `claim.py` (lease `FOR UPDATE SKIP LOCKED`, attempt counter),
  `dataset.py` (manifest → pages → verify → re-verify `dataset_id`), `render.py` (ReportLab, no HTML/URL fetching,
  escaped text, synthetic watermark), `store.py` (deterministic object key = request id + dataset id, checksum,
  upload-then-commit, idempotent re-upload), `main.py` (`--drain` finite mode for Jobs, `--max-messages`).
* `gateway/` — HTTP-aware reverse proxy (Python/ASGI or nginx config) after TLS termination: `/api/auth/*` → core,
  `/x/*` → extension APIs, strips client identity headers.
* `portal/` — React/TS pages: Statements (request, list, status, download).
* Tests: authorization (A vs B), idempotency, crash points (after dequeue / after upload / after DB commit before ack),
  >10k entries, boundaries, empty period.

## M3 — batch statements

* `batch/` service or `statements-api` batch endpoints: manifest freeze (`batch_members` table), cutoff, progress
  counters, retry of failed members only, scheduled trigger = same operation (CronJob calling the API).
* `deploy/openshift/statements-worker-scaledjob.yaml` (KEDA, after infra confirms CRD version).
* Local load test (k6) with bounded concurrency; measure core latency before/during.

## M4 — loans workflow

* `services/loans-api/` — drafts, submit, assignment, review/recommendation, decision with reviewer ≠ approver,
  optimistic concurrency (`version`), append-only `loan_events` (no UPDATE/DELETE grant for app role),
  `extension_role_assignments` keyed by core subject (no core role change).
* `migrations/loans/0001_initial.py`.

## M5 — loan summary worker

* `services/loans-worker/` — separate queue, uses `summaries:read` (manifest only), synthetic labelled scoring.

## M6 — package

* `deploy/openshift/` (namespace `nvb-digital`, SAs, NetworkPolicies, Secrets templates, migration Jobs,
  Deployments, Routes, ScaledJobs), `docs/RUNBOOK.md` (order, rollback, DB compatibility), cluster test plan.
