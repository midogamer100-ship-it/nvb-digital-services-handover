# Remaining blockers and open decisions

Only genuinely unresolved items. Everything else is implemented and tested as stated in
`docs/TESTING.md`.

## Blocking the isolated OpenShift test deployment

| # | Item | Why it blocks | Who | Proposed default |
| --- | --- | --- | --- | --- |
| B1 | **No image has been built.** The authoring workstation has no container engine (no podman/docker/WSL). Containerfiles were verified structurally and by running every command from the assembled image layout (`scripts/verify_image_layout.py`), not by a build. | Manifests reference images that do not exist in any registry yet | whoever has podman + registry access | run `scripts/build-images.sh` (runbook step 5); expect minor fixes on first build (base image contents, e.g. `python3` in the nginx base) |
| B2 | Registry, image names and digests | `REPLACE_REGISTRY` / `REPLACE_VERSION` / `REPLACE_CORE_VERSION` in `overlays/test/images` | infra | internal registry of the test namespace |
| B3 | Portal hostname for the test Route | `REPLACE_TEST_PORTAL_HOST` | infra | `nvb-digital-test.apps.<cluster domain>` |
| B4 | Approval of the three disposable dependency images (PostgreSQL 16 sclorg, RabbitMQ 3.13, SeaweedFS 3.97) or approved mirrors/alternatives, and their behaviour under the restricted SCC | never run on OpenShift by us; RabbitMQ and SeaweedFS upstream images under an arbitrary UID are the likeliest to need adjustment | infra | as listed; any S3-compatible store and any RabbitMQ ≥ 3.13 with quorum queues work |
| B5 | StorageClass and capacity confirmation on the single node | PVCs use the cluster default class; resource numbers are assumptions | infra | approve default class or name the class; 0.7 CPU / 1.7 GiB requests |

## Blocking the cutover to the existing core (runbook Part B)

| # | Item | Who |
| --- | --- | --- |
| C1 | Revision currently running on the core VMs, and the Git ref / bootstrap mechanism they follow. The patch is based on `ffd3201`; if the VMs run something else the patch must be rebased and re-tested. | infra |
| C2 | Decision to roll the additive core patch out (flag off → canary → fleet) and how future clones receive it; whether `/internal/` is additionally restricted at the VM nginx/HAProxy | infra + core owner |
| C3 | Core load balancer hostname and the CA of its certificate. Because the existing route is TLS passthrough, the names in the VM/HAProxy certificate must match the hostname the adapter and gateway use; otherwise a correct certificate or DNS name is needed (verification cannot be disabled). | infra |
| C4 | PoC-grade PostgreSQL, RabbitMQ (TLS) and S3 endpoints with separate roles/vhost/bucket, their CA bundle and network addresses (`REPLACE_*` in `overlays/poc`) | infra |
| C5 | KEDA: installed? which version/API? namespace permissions and network path to RabbitMQ. Until confirmed, use the manual worker Jobs (fully supported path). | infra |
| C6 | Portal hostname + certificate for the shared PoC | infra |

## Open business decisions (defaults implemented; change is configuration only)

* Statement period = calendar month in `Africa/Cairo` (`STATEMENTS_STATEMENT_TIMEZONE`).
* Who may assign reviewers: `approver` or `extension_admin` (code).
* Retention of generated PDFs and audit rows: nothing is deleted automatically; a retention
  policy has not been defined.

## Known limitations (not blockers)

* Not validated on OpenShift: nothing in `deploy/openshift` has been applied to a cluster. It was
  rendered with kustomize, schema-checked with kubeconform and policy-checked offline.
* Test overlay dependencies are single-replica and unencrypted inside the namespace (explicit,
  flagged, synthetic data). The PoC overlay requires TLS endpoints.
* Single node: nothing here is highly available; the extension shares CPU, memory and the
  failure domain with the core VMs.
* The core sees the gateway as the client address for login throttling/audit (see
  `docs/DESIGN.md` §2).
* Per-service RabbitMQ permission regexes written by `generate_credentials.py` are stricter than
  the single test user used locally (`^nvb\..*`); the broker behaviour they rely on (write on
  `amq.default`, configure on `nvb.dlx`) was found and verified locally, the exact per-service
  regexes were not.
* Load numbers: only laptop observations exist; no capacity figure for the cluster is claimed.
