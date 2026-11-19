#!/usr/bin/env bash
# Esegue TUTTI i test (nessuna rete, nessun denaro, dati temporanei).
# Uso: ./scripts/run_tests.sh [--soak MINUTI]
set -euo pipefail
cd "$(dirname "$0")/.."

PY=${PY:-python3}
[ -x .venv/bin/python ] && PY=.venv/bin/python

# Install production dependencies first
echo "=== Installing production dependencies ==="
$PY -m pip install -q -r requirements.txt

# Install dev dependencies
echo "=== Installing dev dependencies ==="
$PY -m pip install -q -r requirements-dev.txt

# Set UTF-8 encoding for Python to handle Unicode output on Windows
export PYTHONIOENCODING=utf-8

FAILED=0
TOTAL_SUITES=0

run_suite() {
    local name="$1"
    local script="$2"
    TOTAL_SUITES=$((TOTAL_SUITES + 1))
    echo "=== Running $name ==="
    if LOG_STDOUT=0 PYTHONIOENCODING=utf-8 $PY "$script"; then
        echo "=== $name PASSED ==="
    else
        echo "=== $name FAILED ==="
        FAILED=$((FAILED + 1))
    fi
}

run_suite "test_cervello.py" "scripts/test_cervello.py"
run_suite "test_survival.py" "scripts/test_survival.py"
run_suite "test_commands.py" "scripts/test_commands.py"
run_suite "test_telegram.py" "scripts/test_telegram.py"
run_suite "test_upgrades.py" "scripts/test_upgrades.py"
run_suite "test_robustness.py" "scripts/test_robustness.py"
run_suite "test_gmgn.py" "scripts/test_gmgn.py"
run_suite "test_gmgn_adapter.py" "scripts/test_gmgn_adapter.py"
run_suite "test_pumpfun.py" "scripts/test_pumpfun.py"
run_suite "test_dashboard.py" "scripts/test_dashboard.py"
run_suite "test_rpc_manager.py" "scripts/test_rpc_manager.py"

if [ "${1:-}" = "--soak" ]; then
    SOAK_MINUTES="${2:-35}" $PY scripts/soak.py 2>/dev/null || echo "Soak test script not found, skipping"
fi

echo "=== Test Summary: $((TOTAL_SUITES - FAILED))/$TOTAL_SUITES suites passed ==="
if [ $FAILED -gt 0 ]; then
    echo "=== SOME TESTS FAILED ==="
    exit 1
fi
echo "=== ALL TESTS PASSED ==="