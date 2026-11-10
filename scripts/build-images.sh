#!/usr/bin/env bash
# Build (and optionally push) every image from a CLEAN, COMMITTED checkout and record digests.
#
#   REGISTRY=registry.example/nvb VERSION=0.2.0 scripts/build-images.sh           # build only
#   REGISTRY=registry.example/nvb VERSION=0.2.0 PUSH=true scripts/build-images.sh # build + push
#
# Code checkout, image build and deployment are three separate steps: this script never deploys,
# and running containers never pull code. Pushing needs explicit authorization (PUSH=true) and a
# prior `podman login`. Output: dist/images-<version>.txt (image references with digests) to be
# pasted into the kustomize overlay.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
: "${REGISTRY:?set REGISTRY, e.g. image-registry.openshift-image-registry.svc:5000/nvb-digital-test}"
: "${VERSION:?set VERSION, e.g. 0.2.0}"
ENGINE="${ENGINE:-podman}"
PUSH="${PUSH:-false}"
if [ -n "$(git status --porcelain)" ] && [ "${ALLOW_DIRTY:-false}" != "true" ]; then
  echo "Refused: the working tree has uncommitted changes (set ALLOW_DIRTY=true for a throw-away build)." >&2
  exit 1
fi
REVISION="$(git rev-parse HEAD)"
mkdir -p dist
OUT="dist/images-${VERSION}.txt"
{ echo "# source revision: ${REVISION}"; echo "# built: $(date -u +%Y-%m-%dT%H:%M:%SZ)"; } > "$OUT"
for spec in "nvb-core-adapter:services/core-adapter/Containerfile" "nvb-statements:services/statements/Containerfile" \
            "nvb-loans:services/loans/Containerfile" "nvb-gateway:gateway/Containerfile"; do
  name="${spec%%:*}"; file="${spec#*:}"; ref="${REGISTRY}/${name}:${VERSION}"
  echo "== building ${ref} from ${file}"
  "$ENGINE" build -f "$file" --build-arg "REVISION=${REVISION}" --build-arg "VERSION=${VERSION}" -t "$ref" .
  if [ "$PUSH" = "true" ]; then
    "$ENGINE" push --digestfile "dist/${name}.digest" "$ref"
    echo "${REGISTRY}/${name}@$(cat "dist/${name}.digest")  # ${VERSION}" >> "$OUT"
  else
    echo "${ref}  # local build, not pushed (image id $("$ENGINE" image inspect --format '{{.Id}}' "$ref"))" >> "$OUT"
  fi
done
echo "Recorded in ${OUT}:"; cat "$OUT"
