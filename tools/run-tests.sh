#!/usr/bin/env bash
# Runs every test suite on CPU, in Docker. No GPU is touched, and each container has a hard memory limit,
# so this is safe to run on the laptop.
#   docker build -t ekido-test:cpu -f infra/test-cpu.Dockerfile infra
#   bash infra/run-tests.sh
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export MSYS_NO_PATHCONV=1
HOST_ROOT="$ROOT"
command -v cygpath >/dev/null 2>&1 && HOST_ROOT="$(cygpath -m "$ROOT")"
FAILED=0
LIMITS=(--memory "${TEST_MEMORY:-4g}" --memory-swap "${TEST_MEMORY:-4g}" --cpus "${TEST_CPUS:-6}")
FILTER='ResourceWarning|tracemalloc|StarletteDeprecation|TestClient as TestClient'

suite() {
  local name="$1" dir="$2" path="$3"
  echo "=== $name"
  docker run --rm "${LIMITS[@]}" -v "$HOST_ROOT:/repo" -w "/repo/$dir" -e "PYTHONPATH=$path" ekido-test:cpu \
    python -m unittest discover -s tests 2>&1 | tail -n "${TAIL:-25}" | grep -vE "$FILTER"
  [ "${PIPESTATUS[0]}" -eq 0 ] || FAILED=1
}

e2e() {
  local name="$1" file="$2"
  echo "=== $name"
  docker run --rm "${LIMITS[@]}" -v "$HOST_ROOT:/repo" -w /repo -e PYTHONDONTWRITEBYTECODE=1 \
    -e PYTHONPATH=/repo:apps/coordinator:packages/ekido_core:workers/agent:workers/runtime:workers/verifier ekido-test:cpu \
    python -m unittest "$file" 2>&1 | tail -n "${TAIL:-25}" | grep -vE "$FILTER"
  [ "${PIPESTATUS[0]}" -eq 0 ] || FAILED=1
}

CORE=/repo/packages/ekido_core
suite "core" packages/ekido_core ".:tests"
suite "runtime" workers/runtime ".:$CORE"
suite "agent" workers/agent ".:$CORE"
suite "coordinator" apps/coordinator ".:$CORE"
[ -d "$ROOT/workers/verifier/tests" ] && suite "verifier" workers/verifier ".:$CORE:/repo/workers/runtime"

e2e "end to end: recovery (real server, agents and kills)" tests/test_e2e_recovery.py
e2e "end to end: every fault" tests/test_e2e_faults.py

echo "=== settlement on a local chain (real transactions)"
docker run --rm -v "$HOST_ROOT:/repo" -w /repo/contracts --entrypoint forge ghcr.io/foundry-rs/foundry:latest build >/dev/null 2>&1
docker rm -f ekido-anvil-e2e >/dev/null 2>&1
docker run -d --rm --name ekido-anvil-e2e -p 127.0.0.1:18545:8545 --memory 256m --entrypoint anvil ghcr.io/foundry-rs/foundry:latest --host 0.0.0.0 --silent >/dev/null
sleep 3
e2e "settlement" tests/test_e2e_settlement.py
docker rm -f ekido-anvil-e2e >/dev/null 2>&1

# This one starts its own chain with the docker command, so it runs on the host, not in a container.
echo "=== chain client against a real local chain"
HOST_PYTHON="${HOST_PYTHON:-$ROOT/.venv/Scripts/python.exe}"
SEP=":"; command -v cygpath >/dev/null 2>&1 && SEP=";"
if "$HOST_PYTHON" -c "import web3" >/dev/null 2>&1; then
  (cd "$ROOT" && PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="apps/coordinator${SEP}packages/ekido_core${SEP}workers/verifier" "$HOST_PYTHON" -m unittest tests/test_e2e_chain.py 2>&1) | tail -n "${TAIL:-25}" | grep -vE "$FILTER"
  [ "${PIPESTATUS[0]}" -eq 0 ] || FAILED=1
else
  echo "SKIPPED: no Python with web3 at $HOST_PYTHON. Set HOST_PYTHON to run it."
fi

echo "=== contracts"
docker run --rm -v "$HOST_ROOT:/repo" -w /repo/contracts --entrypoint forge ghcr.io/foundry-rs/foundry:latest test 2>&1 | tail -n 1
[ "${PIPESTATUS[0]}" -eq 0 ] || FAILED=1

[ "$FAILED" -eq 0 ] && echo "ALL SUITES PASSED" || echo "SOME SUITES FAILED"
exit "$FAILED"
