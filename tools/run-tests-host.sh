#!/usr/bin/env bash
# Every test suite with no Docker and no GPU, for a laptop short of memory. Slower than nothing, far lighter than Docker.
#
#   bash infra/setup-host.sh       # once
#   bash infra/run-tests-host.sh   # all of it, about 15 minutes
#   bash infra/run-tests-host.sh fast   # skips the long end-to-end runs
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
PY="$ROOT/.cache/cpu-venv/Scripts/python.exe"
[ -x "$PY" ] || PY="$ROOT/.cache/cpu-venv/bin/python"
BIN="$ROOT/.cache/foundry"
[ -x "$PY" ] || { echo "Run bash infra/setup-host.sh first"; exit 1; }
SEP=":"; command -v cygpath >/dev/null 2>&1 && { SEP=";"; ROOT="$(cygpath -m "$ROOT")"; }
export PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1
FILTER='ResourceWarning|tracemalloc|StarletteDeprecation|TestClient as TestClient'
FAILED=0

suite() {
  local name="$1" dir="$2" path="$3"
  echo "=== $name"
  (cd "$dir" && PYTHONPATH="$path" "$PY" -m unittest discover -s tests 2>&1) | tail -n "${TAIL:-15}" | grep -vE "$FILTER"
  [ "${PIPESTATUS[0]}" -eq 0 ] || FAILED=1
}

e2e() {
  local name="$1" file="$2"
  echo "=== $name"
  PYTHONPATH="$ROOT${SEP}$ROOT/apps/coordinator${SEP}$ROOT/packages/gpuchain_core${SEP}$ROOT/workers/agent${SEP}$ROOT/workers/runtime${SEP}$ROOT/workers/verifier" \
    "$PY" -m unittest "$file" 2>&1 | tail -n "${TAIL:-25}" | grep -vE "$FILTER"
  [ "${PIPESTATUS[0]}" -eq 0 ] || FAILED=1
}

CORE="$ROOT/packages/gpuchain_core"
suite "core" packages/gpuchain_core ".${SEP}tests"
suite "runtime" workers/runtime ".${SEP}$CORE"
suite "agent" workers/agent ".${SEP}$CORE"
suite "coordinator" apps/coordinator ".${SEP}$CORE"
suite "verifier" workers/verifier ".${SEP}$CORE${SEP}$ROOT/workers/runtime"

echo "=== contracts"
(cd contracts && "$BIN/forge" test 2>&1) | tail -n 1
[ "${PIPESTATUS[0]}" -eq 0 ] || FAILED=1

if [ "${1:-}" != "fast" ]; then
  e2e "end to end: recovery (real server, agents and kills)" tests/test_e2e_recovery.py
  e2e "end to end: every fault" tests/test_e2e_faults.py
  (cd contracts && "$BIN/forge" build >/dev/null 2>&1)
  "$BIN/anvil" --port 18545 --silent >/dev/null 2>&1 &
  ANVIL=$!
  sleep 2
  export ANVIL_RPC="http://127.0.0.1:18545"
  e2e "settlement on a local chain (real transactions)" tests/test_e2e_settlement.py
  kill "$ANVIL" 2>/dev/null
  wait "$ANVIL" 2>/dev/null
  e2e "chain client against a real local chain" tests/test_e2e_chain.py
fi

[ "$FAILED" -eq 0 ] && echo "ALL SUITES PASSED" || echo "SOME SUITES FAILED"
exit "$FAILED"
