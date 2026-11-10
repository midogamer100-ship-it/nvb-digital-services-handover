#!/usr/bin/env bash
# Runs every LOCAL suite that needs no external service (never touches shared infrastructure)
# and reports a non-zero exit if any fails. Real-dependency suites are separate:
#   python scripts/run_integration_tests.py            (disposable PostgreSQL / RabbitMQ / S3)
#   python scripts/local_run.py up && python -m pytest tests/gateway -p no:cacheprovider -o addopts= --import-mode=importlib -q
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PY:-$ROOT/.venv/Scripts/python}"; [ -x "$PY" ] || PY="$ROOT/.venv/bin/python"
CORE="${CORE_BACKEND_DIR:-$ROOT/../NVB-Monolith-integration/backend}"
unset NVB_TEST_DATABASE_URL
rc=0
"$PY" "$ROOT/scripts/safe_core_tests.py" --core-dir "$CORE" -- -p no:cacheprovider -o addopts="" -q -rf || rc=1
(cd "$ROOT/services/core-adapter" && "$PY" -m pytest) || rc=1
(cd "$ROOT/services/statements" && "$PY" -m pytest) || rc=1
(cd "$ROOT/services/loans" && "$PY" -m pytest) || rc=1
(cd "$ROOT" && CORE_BACKEND_DIR="$CORE" "$PY" -m pytest) || rc=1
"$PY" "$ROOT/scripts/verify_image_layout.py" || rc=1
exit $rc
