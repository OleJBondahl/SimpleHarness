#!/usr/bin/env bash
# SimpleHarness dev-container launcher.
#
# Usage:
#   scripts/launch.sh                             # worksite = current dir
#   scripts/launch.sh --worksite /path/to/repo    # explicit worksite
#   scripts/launch.sh --allow-toolbox-edits       # mount /opt/simpleharness RW
#
# Validates the worksite, builds the image (cached), probes credentials,
# then runs `docker compose run` with a TTY so the harness's Ctrl+C → correction
# prompt flow works.
set -euo pipefail

# --- locate the SimpleHarness repo (this script lives at <repo>/scripts/) ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLBOX_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# --- parse args ---
WORKSITE=""
ALLOW_EDITS=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --worksite)            WORKSITE="$2"; shift 2 ;;
    --allow-toolbox-edits) ALLOW_EDITS=1; shift ;;
    -h|--help)             sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "[launch] unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -z "${WORKSITE}" ]] && WORKSITE="$(pwd)"

# --- validate worksite is a git repo ---
if [[ ! -d "${WORKSITE}/.git" ]]; then
  echo "[launch] ${WORKSITE} is not a git repo — the harness needs one to commit" >&2
  exit 1
fi

# --- self-deletion guard: worksite must not overlap the toolbox ---
WORKSITE_REAL="$(cd "${WORKSITE}" && pwd -P)"
TOOLBOX_REAL="$(cd "${TOOLBOX_DIR}" && pwd -P)"
if [[ "${WORKSITE_REAL}" == "${TOOLBOX_REAL}" ]] \
   || [[ "${WORKSITE_REAL}" == "${TOOLBOX_REAL}"/* ]] \
   || [[ "${TOOLBOX_REAL}" == "${WORKSITE_REAL}"/* ]]; then
  echo "[launch] refusing: worksite overlaps the SimpleHarness toolbox" >&2
  echo "[launch]   worksite: ${WORKSITE_REAL}" >&2
  echo "[launch]   toolbox:  ${TOOLBOX_REAL}" >&2
  echo "[launch] (dangerous mode could delete the toolbox itself)" >&2
  exit 1
fi

# --- normalize paths for Docker Desktop on Windows ---
export MSYS_NO_PATHCONV=1
if command -v cygpath >/dev/null 2>&1; then
  WORKSITE_PATH="$(cygpath -m "${WORKSITE_REAL}")"
  TOOLBOX_PATH="$(cygpath -m "${TOOLBOX_REAL}")"
else
  WORKSITE_PATH="${WORKSITE_REAL}"
  TOOLBOX_PATH="${TOOLBOX_REAL}"
fi
export WORKSITE_PATH

# --- per-worksite compose project name (enables parallel instances) ---
HASH=$(printf '%s' "${WORKSITE_PATH}" | sha1sum | cut -c1-8)
export COMPOSE_PROJECT_NAME="sh-${HASH}"

# --- toolbox mount mode ---
if [[ "${ALLOW_EDITS}" -eq 1 ]]; then
  export TOOLBOX_MOUNT_RO=false
  echo "[launch] --allow-toolbox-edits: /opt/simpleharness mounted RW"
else
  export TOOLBOX_MOUNT_RO=true
fi

# --- cd into toolbox so compose finds compose.yml ---
cd "${TOOLBOX_PATH}"

# --- build (cached no-op after first run) ---
echo "[launch] building image (cached after first run)..."
docker compose build

# --- probe credentials volume ---
HAS_CREDS=$(docker compose run --rm --no-deps --entrypoint sh simpleharness \
  -c '[ -f /home/harness/.claude/.credentials.json ] && echo yes || echo no' \
  2>/dev/null | tr -d '\r' | tail -n1 || echo no)

if [[ "${HAS_CREDS}" != "yes" ]]; then
  cat <<EOF
[launch] Claude Code credentials not found in the persistent volume.
[launch] Run this once to log in:

    cd "${TOOLBOX_PATH}"
    WORKSITE_PATH="${WORKSITE_PATH}" COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME}" docker compose run --rm --entrypoint claude simpleharness login

[launch] Then re-run scripts/launch.sh.
EOF
  exit 1
fi

# --- launch ---
echo "[launch] worksite = ${WORKSITE_PATH}"
echo "[launch] project  = ${COMPOSE_PROJECT_NAME}"
echo "[launch] dropping into container shell — run 'simpleharness watch' when ready"
exec docker compose run --rm simpleharness bash
