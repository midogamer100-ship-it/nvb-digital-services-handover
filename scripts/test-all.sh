#!/usr/bin/env bash
# Runs every local suite (never touches shared infrastructure) and reports each result.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PY:-$ROOT/.venv/Scripts/python}"; [ -x "$PY" ] || PY="$ROOT/.venv/bin/python"
CORE="${CORE_BACKEND_DIR:-$ROOT/../NVB-Monolith-integration/backend}"
unset NVB_TEST_DATABASE_URL
rc=0
"$PY" "$ROOT/scripts/safe_core_tests.py" --core-dir "$CORE" -- -p no:cacheprovider -o addopts="" -q -rf || rc=1
(cd "$ROOT/services/core-adapter" && "$PY" -m pytest) || rc=1
(cd "$ROOT/services/statements" && "$PY" -m pytest) || rc=1
(cd "$ROOT" && CORE_BACKEND_DIR="$CORE" "$PY" -m pytest) || rc=1
exit $rc
