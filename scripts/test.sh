#!/usr/bin/env bash
# Run lint and tests. Install deps if missing.
# Usage:
#   ./scripts/test.sh           Run all checks
#   ./scripts/test.sh lint      Lint only
#   ./scripts/test.sh test      Tests only
#   ./scripts/test.sh quick     Lint + fast tests (no integration)
set -euo pipefail

cd "$(dirname "$0")/.."

_ensure_deps() {
    if ! python3 -c "import pytest, ruff" 2>/dev/null; then
        echo "Installing test dependencies..."
        pip3 install --break-system-packages -q -r requirements-dev.txt 2>/dev/null || \
        pip3 install -q -r requirements-dev.txt
    fi
}

cmd_lint() {
    echo "=== ruff check ==="
    ruff check agents/ tools/ tests/ relay/
    echo "OK"
}

cmd_test() {
    echo "=== pytest ==="
    pytest tests/ -q --tb=short
}

cmd_quick() {
    cmd_lint
    echo ""
    echo "=== pytest (unit only) ==="
    pytest tests/ -q --tb=short -k "not integration and not Integration"
}

_ensure_deps

case "${1:-all}" in
    lint)  cmd_lint ;;
    test)  cmd_test ;;
    quick) cmd_quick ;;
    all)   cmd_lint; echo ""; cmd_test ;;
    *)     echo "Usage: $0 [lint|test|quick|all]"; exit 1 ;;
esac
