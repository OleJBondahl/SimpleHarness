#!/usr/bin/env bash
# Run the full CI pipeline locally.
# Usage: bash scripts/ci-local.sh [--mutate]
set -uo pipefail

cd "$(git rev-parse --show-toplevel)"

failures=0

run_step() {
    echo "=== $1 ==="
    shift
    if ! "$@"; then
        failures=$((failures + 1))
    fi
}

run_step "Lint" uv run ruff check .
run_step "Format" uv run ruff format --check .
run_step "Type check" uv run ty check
run_step "Architecture boundaries" uv run lint-imports
run_step "Dead code" uv run vulture
run_step "Deal lint (FP purity)" uv run python -X utf8 -m deal lint src/simpleharness/core.py src/simpleharness/approver_core.py
run_step "FP purity gate" uv run python -X utf8 scripts/check_fp_purity.py src/simpleharness/core.py src/simpleharness/approver_core.py
run_step "Deal runtime contracts" uv run python -X utf8 scripts/check_deal_runtime.py
run_step "Codebase map" npx codesight --wiki
run_step "Detect secrets" uv run --group security detect-secrets-hook --baseline .secrets.baseline $(git ls-files -- '*.py' '*.md' '*.yaml' '*.yml' '*.toml' '*.json' '*.sh')
run_step "Tests" uv run pytest
run_step "Core coverage (90% minimum)" uv run pytest --override-ini="addopts=-ra --strict-markers" --cov --cov-config=core-coverage.ini -q --no-header
run_step "Prose linting" vale --minAlertLevel=error docs/ README.md

if [[ "${1:-}" == "--mutate" ]]; then
    echo "=== Mutation testing (via WSL) ==="
    wsl -d Ubuntu -- bash -c "
        cd /mnt/c/Users/OleJohanBondahl/Documents/Github_OJ/SimpleHarness
        rm -rf .mutmut-cache .venv
    "
    if ! wsl -d Ubuntu -- bash -c "
        source \$HOME/.local/bin/env
        cd /mnt/c/Users/OleJohanBondahl/Documents/Github_OJ/SimpleHarness
        export UV_PROJECT_ENVIRONMENT=\$HOME/simpleharness-ci/.venv
        uv run python -m mutmut run
        if [ \$? -eq 0 ]; then
            uv run python -m mutmut results
        else
            echo 'mutmut run failed — skipping results'
            exit 1
        fi
    "; then
        failures=$((failures + 1))
    fi
fi

if [ "$failures" -gt 0 ]; then
    echo "=== $failures step(s) FAILED ==="
    exit 1
else
    echo "=== All checks passed ==="
fi
