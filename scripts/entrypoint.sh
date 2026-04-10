#!/usr/bin/env bash
# SimpleHarness container entrypoint. Runs as the `harness` user.
#
# First-run bootstrap: installs simpleharness from the bind-mounted toolbox,
# sets container-local git config, initializes the worksite scaffold, then
# execs the CMD (interactive bash shell via launch.sh).
set -euo pipefail

export PATH="/home/harness/.local/bin:${PATH}"

# Container-local git config. autocrlf=input prevents CRLF-on-Windows-host
# bind-mounted files from looking "modified" on `git status`. safe.directory
# sidesteps ownership mismatches on bind mounts.
git config --global core.autocrlf input
git config --global --add safe.directory /worksite
git config --global --add safe.directory /opt/simpleharness

# First-run install of the harness. No-op on subsequent runs because the
# uv tool venv lives in the persistent home volume. Copies to /tmp first
# because setuptools needs to write egg-info and /opt/simpleharness may
# be read-only.
if ! command -v simpleharness >/dev/null 2>&1; then
  echo "[entrypoint] installing simpleharness from /opt/simpleharness ..."
  cp -r /opt/simpleharness /tmp/simpleharness-build
  uv tool install /tmp/simpleharness-build
  rm -rf /tmp/simpleharness-build
fi

# First-run worksite init.
if [[ ! -d /worksite/simpleharness ]]; then
  echo "[entrypoint] running simpleharness init on /worksite ..."
  simpleharness init --worksite /worksite
fi

# Note: per-worksite config.yaml is NOT auto-generated. The user creates it
# (or `simpleharness init` does) with their chosen permission mode.
# To use dangerous mode inside the container, create the config manually:
#   echo 'permissions:\n  mode: dangerous' > /worksite/simpleharness/config.yaml

echo "[entrypoint] python=$(python3 --version 2>&1)  node=$(node --version)  uv=$(uv --version | awk '{print $2}')  claude=$(claude --version 2>&1 | head -n1)"
exec "$@"
