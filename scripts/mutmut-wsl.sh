#!/usr/bin/env bash
# Run mutmut inside WSL. Called by ci-local.sh --mutate.
set -uo pipefail

source "$HOME/.local/bin/env"
cd /mnt/c/Users/OleJohanBondahl/Documents/Github_OJ/SimpleHarness
export UV_PROJECT_ENVIRONMENT="$HOME/simpleharness-ci/.venv"

uv run python -m mutmut run 2>&1 | tail -100
mutmut_rc=${PIPESTATUS[0]}

if [ "$mutmut_rc" -ne 0 ]; then
    echo "mutmut run failed (exit $mutmut_rc) — see output above"
    exit 1
fi

survived=$(uv run python -m mutmut results | grep -c ': survived$' || true)
killed=$(uv run python -m mutmut results | grep -c ': killed$' || true)
unchecked=$(uv run python -m mutmut results | grep -c ': not checked$' || true)
echo "Killed: $killed  Survived: $survived  Unchecked: $unchecked"

if [ "$survived" -gt 0 ]; then
    echo "Survived mutants — run 'mutmut results' in WSL for details"
    exit 1
fi
