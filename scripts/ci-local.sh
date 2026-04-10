#!/usr/bin/env bash
# Run the full CI pipeline locally.
# Usage: bash scripts/ci-local.sh [--mutate]
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

echo "=== Lint ==="
uv run ruff check .

echo "=== Format ==="
uv run ruff format --check .

echo "=== Type check ==="
uv run ty check

echo "=== Architecture boundaries ==="
uv run lint-imports

echo "=== Dead code ==="
uv run vulture

echo "=== Deal lint (FP purity) ==="
uv run python -X utf8 -m deal lint src/simpleharness/core.py src/simpleharness/approver_core.py

echo "=== FP purity gate ==="
uv run python -X utf8 scripts/check_fp_purity.py src/simpleharness/core.py src/simpleharness/approver_core.py

echo "=== Deal runtime contracts ==="
uv run python -X utf8 scripts/check_deal_runtime.py

echo "=== Codebase map ==="
npx codesight --wiki

echo "=== Detect secrets ==="
uv run --group security detect-secrets-hook --baseline .secrets.baseline $(git ls-files -- '*.py' '*.md' '*.yaml' '*.yml' '*.toml' '*.json' '*.sh')

echo "=== Tests ==="
uv run pytest

echo "=== Core coverage (90% minimum) ==="
uv run pytest --override-ini="addopts=-ra --strict-markers" --cov --cov-config=core-coverage.ini -q --no-header

echo "=== Prose linting ==="
vale --minAlertLevel=error docs/ README.md

if [[ "${1:-}" == "--mutate" ]]; then
    echo "=== Mutation testing (via WSL) ==="
    wsl -d Ubuntu -- bash -c "
        source \$HOME/.local/bin/env
        cd /mnt/c/Users/OleJohanBondahl/Documents/Github_OJ/SimpleHarness
        UV_PROJECT_ENVIRONMENT=\$HOME/simpleharness-ci/.venv \
        uv run python -m mutmut run
        uv run python -m mutmut results
    "
fi

echo "=== All checks passed ==="
