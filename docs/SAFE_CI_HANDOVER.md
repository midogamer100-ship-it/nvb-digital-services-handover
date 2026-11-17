# Safe CI bootstrap - no deployment

This branch adds hosted-runner checks and candidate builds, not production CD.
No Kubernetes, SSH, registry pull, database or live rollout credentials are required for CI.
No push, tag, merge or live action was performed when preparing these files.

## Activation

Review and push this feature branch, then run its CI. Workflows use hosted runners only.
Do not merge into main until the tested integration/admin base commits are reviewed together.
Protected build/release orchestration is not included in this first CI stage. Configure
required reviews and trusted branch restrictions before granting LAN runner access.
Environment creation by a workflow does NOT create protection automatically.
Actions are version-tagged; pin verified commits before granting production credentials.
Registry base images are mutable; approve base digests before release promotion.

Candidate publication will use dedicated BuildConfigs in nvb-ci-builds and the existing
OpenShift internal registry. No GHCR workflow is included. The build bot has no deployment
permission. CI success is not production approval. Internal builds are reviewed/manual
until runner scope and authentication are configured. Never deploy mutable candidate tags.

## Testing limits

SQLite and fake-dependency tests are local correctness checks. They do not establish real
PostgreSQL locking behavior, RabbitMQ/S3 behavior, nginx TLS/Origin behavior or cross-service
compatibility. A disposable integration environment and paired Core/Digital source SHAs are
required before promotion. No shared namespace may be reset to run these tests.

## CD work deliberately not activated

Collect current runner/cluster state using the read-only collector supplied with this package.
Build a reviewed environment overlay from live resource structure; do not blindly apply the
repository's old test/poc overlay to the working canary namespace.

Production CD must have a protected approval, deployment lock across both repositories,
explicit paired revisions, immutable digests, drift checks, preflight, external rollback records,
and success/failure cleanup. GitHub concurrency groups are repository-local and do not supply
a cross-repository lock. Use a reviewed central release orchestrator or cluster Lease.

Core CD must coordinate versioned Golden source, firstboot constants, BUILD_REVISION and
per-slot bootstrap metadata, not update existing VMs alone. Preserve configuration and data;
do not invoke the old installer/sync or automatically run migrations. Replace one drained VM
at a time only after both public/private load balancers and remaining capacity are verified.
Never pause a pool across deletion of a VM requiring its controller finalizer to complete.
Freeze and restore HPA behavior with state recorded, including failure paths.

Digital CD must coordinate API/relay/worker image versions, including ScaledJob job templates
and scheduled jobs. Updating API Deployments alone leaves workers on older images. Preserve
PVCs, queues, buckets, Secrets, Services/ClusterIPs, CA bundles, Origin checks and hostAliases.
Do not cancel in-flight workers or auto-replay batch/seed/role/migration jobs.

Resolve CA key custody and certificate expiry before relying on automatic fleet creation.
Presigned per-slot leaf certificates do not provide a renewal mechanism.

Rollback must restore code/image references and compatible configuration. Data/schema changes
need a separate reviewed plan; rolling back code does not reverse data mutations.
