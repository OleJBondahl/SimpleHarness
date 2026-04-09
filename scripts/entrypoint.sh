#!/usr/bin/env bash
# SimpleHarness container entrypoint. Runs as the `harness` user.
#
# First-run bootstrap: installs simpleharness from the bind-mounted toolbox,
# sets container-local git config, ensures the worksite is initialized and
# opted in to dangerous mode, then execs the CMD (defaults to `simpleharness watch`).
set -euo pipefail

export PATH="/home/harness/.local/bin:${PATH}"

# Container-local git config. autocrlf=input prevents CRLF-on-Windows-host
# bind-mounted files from looking "modified" on `git status`. safe.directory
# sidesteps ownership mismatches on bind mounts.
git config --global core.autocrlf input
git config --global --add safe.directory /worksite
git config --global --add safe.directory /opt/simpleharness

# First-run install of the harness. No-op on subsequent runs because the
# uv tool venv lives in the persistent home volume.
if ! command -v simpleharness >/dev/null 2>&1; then
  echo "[entrypoint] installing simpleharness from /opt/simpleharness ..."
  uv tool install -e /opt/simpleharness
fi

# First-run worksite init.
if [[ ! -d /worksite/simpleharness ]]; then
  echo "[entrypoint] running simpleharness init on /worksite ..."
  simpleharness init --worksite /worksite
fi

# First-run dangerous-mode opt-in — only if the user has not provided a
# worksite config.yaml themselves. The toolbox config.yaml stays safe, so
# `simpleharness watch` on the host still refuses bypass mode.
WORKSITE_CFG=/worksite/simpleharness/config.yaml
if [[ ! -f "${WORKSITE_CFG}" ]]; then
  echo "[entrypoint] writing dangerous-mode opt-in to ${WORKSITE_CFG}"
  cat > "${WORKSITE_CFG}" <<'EOF'
# Container opt-in to dangerous mode. /.dockerenv satisfies the sandbox
# check at shell.py:699-709 and this override flips the flag.
permissions:
  mode: dangerous
EOF
fi

echo "[entrypoint] python=$(python3 --version 2>&1)  node=$(node --version)  uv=$(uv --version | awk '{print $2}')  claude=$(claude --version 2>&1 | head -n1)"
exec "$@"
