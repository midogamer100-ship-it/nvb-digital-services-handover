# Deployment, validation, rollback and troubleshooting runbook

Audience: the OpenShift infrastructure team. Every step says **what it does and why** before the
command. Nothing here has been executed on OpenShift by the authors: the commands are derived from
manifests that were validated offline (`scripts/validate_manifests.py`) and from code that was
integration-tested locally against real PostgreSQL 16, RabbitMQ 3.13 and an S3-compatible store.
Treat the first run as a test of the runbook itself and record deviations.

Conventions: `NS=nvb-digital-test` (disposable test). `oc` is logged in with a project-admin
account for that namespace only — **cluster-admin is not needed** except to create the namespace
(step 3) and, if chosen, to install/confirm KEDA (infra's decision, step 15). Stop at any failed
check; do not continue with later steps.

Never in this runbook: touching `bank-poc`, the VM pool (`nvb-app-pool`), its HPA, golden image,
source PVC, HAProxy or the existing core Route; `git pull` inside containers; applying the `poc`
overlay (that is step 17, a separate reviewed change).

---

## Part A — isolated test deployment (namespace `nvb-digital-test`)

### 1. Confirm source revisions and image versions
Why: images must be traceable to reviewed commits; the core image must contain the patch.

```
git -C nvb-digital-services rev-parse HEAD                       # extension revision -> image label
git -C NVB-Monolith-integration rev-parse feature/extension-integration-api   # expect 09730f5...
git -C NVB-Monolith-integration merge-base HEAD ffd32015aa00564054bb177f6580196ce1b1355c   # expect ffd3201...
python scripts/validate_manifests.py                              # offline schema + policy checks
python scripts/verify_image_layout.py                             # every COPY source exists; commands import
```
Choose `VERSION` (e.g. `0.2.0`) for the four extension images and a tag for the test core image.

### 2. Validate cluster prerequisites, storage and capacity
Why: single node — pods, databases and the core VMs share CPU, memory and one failure domain.

```
oc version; oc get clusterversion
oc get storageclass                                # which class is default? is it approved for this use?
oc adm top node                                    # free CPU/memory NOW, with the core VMs running
oc get crd scaledjobs.keda.sh triggerauthentications.keda.sh   # KEDA present? (only needed for step 15)
oc get ns nvb-digital-test                         # must NOT exist yet
```
Budget of the test overlay (initial ASSUMPTIONS, not measurements — measure in step 16):
requests ≈ 0.7 CPU / 1.7 GiB, limits ≈ 6.5 CPU / 5.6 GiB with everything running and two workers;
PVCs 2 Gi + 1 Gi + 2 Gi on the default StorageClass. If the default class is not the approved one,
add `storageClassName` to `dependencies-test/dependencies.yaml` volumeClaimTemplates first.
Confirm with infra: the three dependency images (or approved mirrors), the portal hostname, and
whether egress from pods to registries is allowed.

### 3. Create the isolated test namespace
Why: isolation from `bank-poc`; default-deny networking from the first object.

```
oc apply -f deploy/openshift/overlays/test/namespace.yaml          # needs namespace-create rights
oc project nvb-digital-test
```

### 4. Provision disposable dependencies and credentials
Why: credentials are generated locally into a git-ignored directory and loaded from files, so no
secret value appears in Git, in a manifest or on a command line.

```
python scripts/generate_credentials.py --env test --out secrets/test
less secrets/test/create-secrets.sh                                # review
bash secrets/test/create-secrets.sh
oc -n $NS get secrets | grep -v -e token- -e dockercfg             # names only
```
Set the three placeholders of the test overlay (registry, version, portal host, core image tag):
`overlays/test/images/kustomization.yaml`, `overlays/test/config.yaml` (`GATEWAY_PUBLIC_ORIGIN`)
and the Route patch in `overlays/test/kustomization.yaml`; then
`python scripts/validate_manifests.py --strict` must only report the role-bootstrap placeholders.

### 5. Build images and make them available to OpenShift
Why: OpenShift pulls versioned images from a registry; containers never fetch code.
On a machine with podman and a clean checkout (the authors' workstation has no container engine,
so **this step has never been run** — expect to fix small things such as base-image details):

```
podman login <registry>
REGISTRY=<registry>/nvb-digital-test VERSION=0.2.0 PUSH=true scripts/build-images.sh
cat dist/images-0.2.0.txt                                          # digests; paste into the images component
# test core image, from the CORE repository at the integration revision (its own Containerfile):
podman build -f Containerfile -t <registry>/nvb-digital-test/nvb-core:09730f5 ../NVB-Monolith-integration
podman push <registry>/nvb-digital-test/nvb-core:09730f5
```
First-build checks: `podman run --rm <statements image> alembic -c /opt/app/migrations/statements/alembic.ini heads`
prints `0002_statements_batches_roles (head)`; `podman run --rm <gateway image> python3 --version` works.

### 6. Create the objects, then run extension migrations as one-off controlled Jobs
Why: schema changes are explicit, reviewed steps run by the schema-owner role — never at
application start. The overlay is applied as one unit (dependencies, configuration, network
policies and the Deployments); the following steps are **gates**: each verifies one layer before
you rely on the next. Pods whose prerequisites are not there yet are simply not Ready, which is safe
in an isolated namespace with no users.

```
oc apply -k deploy/openshift/overlays/test --dry-run=server        # server-side validation first
oc apply -k deploy/openshift/overlays/test
oc -n $NS rollout status statefulset/postgres statefulset/rabbitmq statefulset/objectstore
oc -n $NS wait --for=condition=complete job/test-deps-init --timeout=600s
oc -n $NS logs job/test-deps-init -c bucket-init                   # "bucket ready: nvb-statements"
oc apply -k deploy/openshift/overlays/test/migrations
oc -n $NS wait --for=condition=complete job/statements-migrate job/loans-migrate --timeout=600s
oc -n $NS logs job/statements-migrate | tail -3                    # "... -> 0002_statements_batches_roles"
oc -n $NS logs job/loans-migrate | tail -3                         # "... -> 0001_loans_initial"
```
For a later release: delete the finished migration Jobs, apply the new ones, wait, then update the
image references of the Deployments.

### 7. Deploy an isolated patched core with synthetic data
Why: validate the integration against a core that shares nothing with the production pool.

```
oc apply -k deploy/openshift/overlays/test/core-test-jobs          # the core's own CLI: migrate, then seed
oc -n $NS wait --for=condition=complete job/core-test-migrate --timeout=600s
oc -n $NS wait --for=condition=complete job/core-test-seed --timeout=900s
oc -n $NS rollout restart deploy/core-test && oc -n $NS rollout status deploy/core-test
```
If `core-test-seed` starts before the migration finished it fails once; delete and re-apply it.
Demo logins are `customer001…`, `opsadmin`; their passwords are in the local file
`secrets/test/test/core-test-seed.env` (never committed).

### 8. Verify the adapter and the authenticated integration
Why: prove credentials, scopes and verified TLS end to end before anything depends on them.
Run from a pod that holds a service token (statements-api); TLS is verified with the service CA.

```
oc -n $NS rollout status deploy/core-adapter deploy/statements-api
oc -n $NS exec -i deploy/statements-api -- python - <<'EOF'
import httpx, ssl
ctx = ssl.create_default_context(cafile="/etc/nvb/service-ca/service-ca.crt")
base = "https://core-adapter.nvb-digital-test.svc:8443"
tok = open("/var/run/secrets/nvb/adapter/token").read().strip()
auth = {"Authorization": "Bearer " + tok}
period = {"subject": "nvb-user:1:1", "period_start": "2026-01-01T00:00:00Z", "period_end": "2026-02-01T00:00:00Z"}
print("ready         ->", httpx.get(base + "/health/ready", verify=ctx).text)
print("no token      ->", httpx.get(base + "/v1/discovery/statement-accounts", verify=ctx).status_code)             # 401
print("bad token     ->", httpx.get(base + "/v1/discovery/statement-accounts", verify=ctx,
                                    headers={"Authorization": "Bearer " + "x" * 40}).status_code)                   # 401
print("wrong scope   ->", httpx.get(base + "/v1/statement-datasets/1/entries", verify=ctx, headers=auth,
                                    params=period).status_code)                                                    # 403
r = httpx.get(base + "/v1/discovery/statement-accounts", verify=ctx, headers=auth, params={"limit": 2})
print("discovery     ->", r.status_code, len(r.json().get("items", [])))                                           # 200 2
EOF
oc -n $NS logs deploy/core-adapter | tail -5        # client ids and outcomes only - never token values
```
`ready` must show `"status":"ready"`. A `502 core_auth_misconfigured` on the last call means the
adapter's own token is not in the core's clients file (Secret `core-integration-clients`).

### 9. Deploy Statements API and Relay
Why: the relay publishes the outbox, declares the queues and recovers abandoned tasks — without it
nothing is processed.

```
oc -n $NS rollout status deploy/statements-api deploy/statements-relay
oc -n $NS logs deploy/statements-relay | grep "queues declared"
```

### 10. Run a worker and verify a complete PDF request
(Do step 11 first if you want to use the browser; with `curl` use the Route host.)
```
# after requesting a statement in the portal (or by API):
oc -n $NS delete job statements-worker-manual --ignore-not-found
oc apply -k deploy/openshift/overlays/test/worker-statements
oc -n $NS wait --for=condition=complete job/statements-worker-manual --timeout=1500s
oc -n $NS logs job/statements-worker-manual | grep -e "statement completed" -e "queues idle"
```
Expected: the request turns `completed`, the PDF downloads, the worker exits 0 by itself.

### 11. Deploy Gateway/Portal and test login/download permissions
```
oc -n $NS rollout status deploy/gateway
oc -n $NS get route nvb-digital -o jsonpath='{.spec.host}{"\n"}'
H=https://$(oc -n $NS get route nvb-digital -o jsonpath='{.spec.host}')
curl -s $H/gateway/health                                  # ok
curl -s -o /dev/null -w '%{http_code}\n' $H/api/accounts   # 404 (not routed)
curl -s -o /dev/null -w '%{http_code}\n' $H/internal/v1/sessions/introspect   # 404
curl -s -o /dev/null -w '%{http_code}\n' $H/x/statements/v1/requests          # 401
```
Browser: sign in as `customer001`, request a statement, run step 10, download; sign in as
`customer002` in a private window and open the first customer's document URL ⇒ "not found".
If the cluster's router certificate is not publicly trusted, use the cluster CA in the browser —
do not click through warnings as the acceptance criterion.

### 12. Deploy Loans and validate its workflow
```
oc -n $NS rollout status deploy/loans-api deploy/loans-relay
```
Bootstrap one `extension_admin` per service (edit a copy of
`deploy/openshift/jobs/role-bootstrap/*.yaml`: subject from the portal's "My identity" page), apply
through the overlay (`overlays/test/role-bootstrap`), then use the portal **Roles** page to grant
`reviewer`, `approver`, `batch_operator`. Walk: customer drafts + submits → approver assigns a
reviewer → run `overlays/test/worker-loans` → reviewer records a recommendation → a different
approver decides. Check that the core balance of the customer did not change.

### 13. Enable batch orchestration
```
oc apply -k deploy/openshift/overlays/test/batch-cronjobs          # created SUSPENDED
oc -n $NS create job --from=cronjob/statements-monthly-batch batch-now   # same operation as the schedule
oc -n $NS logs job/batch-now | tail -1                             # JSON with status "running" and member counts
oc -n $NS patch cronjob statements-monthly-batch loans-summary-batch -p '{"spec":{"suspend":false}}'   # when wanted
```
Or create a batch from the portal **Batches** page (role `batch_operator`).

### 14. Validate manual worker execution
Run `overlays/test/worker-statements` (step 10 commands) repeatedly while the batch is running:
each Job handles at most 50 messages / 15 minutes and exits; progress on the Batches page moves;
the batch ends `completed` or `completed_with_failures` with reasons listed.

### 15. Enable autoscaling only after functional tests pass
Prerequisite (infra): KEDA / Custom Metrics Autoscaler installed, serving `keda.sh/v1alpha1`,
allowed to reach RabbitMQ (add the NetworkPolicy rule for the KEDA namespace) and to read the
`keda-rabbitmq` Secret. We do not install it.
```
oc get crd scaledjobs.keda.sh -o jsonpath='{.spec.versions[*].name}{"\n"}'
oc apply -k deploy/openshift/overlays/test/autoscaling-keda --dry-run=server
oc apply -k deploy/openshift/overlays/test/autoscaling-keda
oc -n $NS get scaledjob,triggerauthentication
```
Do not run manual worker Jobs and ScaledJobs for long at the same time (they would simply share
the queue, but the capacity assumptions double). No HPA exists for any component.

### 16. Run bounded load/failure tests and record results
Use `tests/load/README.md` (finite, capped, refuses non-test targets by default). Record: node and
core VM CPU before/during, core p95 latency, adapter `adapter_core_*` metrics, batch duration,
worker Job count over time (scale up, then back to zero). Failure drills: delete a worker pod
mid-run; stop RabbitMQ for a minute; revoke the adapter token. Expected outcomes are in
`docs/DESIGN.md` ("Failure behaviour"). Write the measured numbers into `docs/TESTING.md`.

---

## Part B — cutover to the existing core (step 17: separate, reviewed plan)

Not part of the test deployment. Prepare and get approval for:

1. **Core code version**: roll the patch out to the VM fleet with the flag off, then on, as
   described in `docs/CORE_PATCH.md` ("Rollout"). This must be coordinated with the Git/bootstrap
   mechanism the VMs use; future clones must receive the approved ref and the integration
   settings. The golden image is not changed by us.
2. **Credentials**: `python scripts/generate_credentials.py --env poc --out secrets/poc`; install
   `core/integration-clients.json` on the core VMs (file + `NVB_INTEGRATION_CLIENTS_FILE`); load
   the namespace Secrets; have the administrators of the PoC PostgreSQL / RabbitMQ / S3 create
   the roles, vhost, permissions and private bucket with those credentials
   (`contracts/extension-apis.md`, "Queues"; database roles as in `dependencies-test/init-job.yaml`).
3. **CA and names**: ConfigMap `core-ca` with the CA of the core load balancer certificate;
   `ADAPTER_CORE_BASE_URL` / `GATEWAY_CORE_UPSTREAM` must use a hostname present in that
   certificate. The existing route is TLS passthrough to the VMs, so the certificate the adapter
   sees is the VM/HAProxy one: if its names do not match the hostname we must use, **issue a
   correct certificate or DNS name** — do not disable verification (the code has no such switch).
4. **Network**: fill the `REPLACE_*_CIDR` egress policies; optionally restrict `/internal/` on
   the VM nginx/HAProxy to the adapter's egress address.
5. **Feature flag**: enable `NVB_INTEGRATION_ENABLED` on a canary VM first.
6. **Apply** `overlays/poc` in the same order as Part A steps 4–15 (no step 6 dependencies, no
   step 7 test core). `python scripts/validate_manifests.py --strict` must pass first.
7. **Capacity**: adapter replicas stay at 1 (fleet bound 4 concurrent core calls, 20 req/s);
   worker `maxReplicaCount` 2 + 1. Watch the core's latency during the first batch.

## Rollback

| What | How | Data |
| --- | --- | --- |
| Extension application images | point the images component back to the previous digests, `oc apply -k` | none lost: requests/tasks stay in the database and are picked up again |
| Workers / autoscaling | `oc delete -k …/autoscaling-keda`; delete worker Jobs | queued work waits; leases of killed workers expire and are re-queued by the relay |
| Batches | portal **Cancel**, or suspend the CronJobs | completed documents are kept |
| Whole extension | scale Deployments to 0 / delete the Route | core unaffected (it has no dependency on the extension) |
| Core integration | `NVB_INTEGRATION_ENABLED=false` + restart, per VM | no schema to reverse |
| Schema | **no automatic downgrade** (disabled in the migrations: it would drop history). Compatibility instead: statements `0002` is additive and image `0.1.x` still works against it; loans `0001` is the first revision. Always migrate first, then roll images forward; an image rollback never needs a schema rollback for these revisions. A future non-additive migration must ship its own expand/contract plan. | |
| Test namespace | `oc delete project nvb-digital-test` (deletes the disposable PVCs) | all test data is gone — intended |

Never automated: deleting business results or audit rows. Completed Jobs are removed by
`ttlSecondsAfterFinished` / history limits; statement objects and database rows are retained
until an explicit retention decision exists.

## Health, logs, metrics

* API liveness `/health/live` (process), readiness `/health/ready` (configuration complete +
  own database reachable). A core/adapter/storage outage does **not** make an API unready — it
  returns explicit 503s for the affected calls; restarting would not help.
* Relays: liveness = heartbeat file refreshed after each database cycle (also while the broker
  is down, because the outbox is safe in the database).
* Startup probes allow ~2 minutes. `terminationGracePeriodSeconds`: APIs 40 s (uvicorn finishes
  in-flight requests), workers 150 s (finish the task in hand), gateway SIGQUIT graceful.
* Logs: JSON on stdout with `request_id` (propagated from the gateway's `X-Request-ID`), never
  tokens/cookies/bodies. Gateway access log: method + path without query strings.
* Metrics: `/metrics` (Prometheus text) on each API pod port 8443 (HTTPS); enable the commented
  monitoring NetworkPolicy + a PodMonitor if infra uses user-workload monitoring. Useful series:
  `adapter_core_requests_total`, `adapter_core_rejected_total`, `statements_worker_tasks_total`,
  `statements_outbox_backlog`, `statements_reconciler_requeued_total`, `loans_worker_tasks_total`.

## Troubleshooting

| Symptom | Check | Usual cause |
| --- | --- | --- |
| Pod not Ready | `oc -n $NS describe pod <pod>` (probe message) and, from another pod with the service CA, `GET https://<service>.<ns>.svc:8443/health/ready` → `problems` list | missing Secret key / ConfigMap value named in the list |
| Pod CrashLoop at start | `oc logs` → `ConfigError: … is not set` / uvicorn SSL error | database URL/password file; `*-tls` Secret not yet issued (re-check the Service annotation) |
| Worker Job exits 78 / 75 | `oc logs job/…` | 78 invalid configuration (list printed); 75 broker unreachable or queues missing (is the relay running?) |
| Requests stay `queued` | `oc logs deploy/statements-relay`; `statements_outbox_backlog` | relay down / broker credentials / no worker Job ran |
| Requests go `failed` `worker_misconfigured` | adapter logs `insufficient_scope` / `invalid_client` | wrong token Secret or scopes in `core-adapter-clients` |
| `core_auth_misconfigured` (502) | adapter log "core rejected adapter credentials" | adapter token not in the core's clients file / flag off on that VM |
| `core_unavailable` with TLS errors | adapter log; `openssl s_client -connect <core>:443 -servername <name> -CAfile ca.crt` from a debug pod | CA bundle or hostname mismatch — fix the certificate/DNS |
| 403 on every POST from the portal | gateway `GATEWAY_PUBLIC_ORIGIN` ≠ browser origin | Route host changed without updating the ConfigMap |
| Login works on the core route but not in the portal | cookies are host-only: a session from the core hostname is not valid on the portal hostname (by design) | sign in on the portal |
| Statement `retry_scheduled` `dataset_incomplete` repeatedly | core has postings landing in a closed period | expected to be rare; it never yields a partial PDF; investigate back-dated postings |
| Messages in `<queue>.dead` | `rabbitmqctl list_queues -p nvb name messages` | malformed/poison messages; inspect, never auto-purge |
| Batch stuck `freezing` | portal **Resume** or re-run the CronJob Job | adapter/core was unavailable mid-discovery (resumable by design) |
