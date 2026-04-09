# SimpleHarness Dev Container — Delivery Workflow

> One-command Docker launch of SimpleHarness against any Python or TypeScript repo, so it can run with `--permission-mode bypassPermissions` safely.

This is a design / spec document. It describes the workflow and names every file to add, but the artifacts themselves are not yet in the repo. When you're ready to implement, copy the code blocks in [§4](#4-files-to-add) into the prescribed paths.

---

## 1. What this is

SimpleHarness runs `claude -p` in headless mode. Its "dangerous mode" (`config.permissions.mode: dangerous` → `--permission-mode bypassPermissions`) approves arbitrary shell, which is unsafe on a dev host. `shell.py:699-709` already gates dangerous mode behind a sandbox marker (`/.dockerenv` or `SIMPLEHARNESS_SANDBOX=1`), but the repo has no container artifacts to satisfy it.

This doc specifies the minimum set of files — one `Dockerfile`, one `compose.yml`, two small shell scripts — that turn any Python or TypeScript git repo into a safe worksite for SimpleHarness running autonomously with full bypass permissions.

**Target user:** solo developer on Windows 11 + Git Bash + Docker Desktop. Linux host support is a future extension (see [§14](#14-future-extensions)).

**In scope:** one-command launch, persistent Claude Code login, per-worksite isolation, multi-instance parallelism, Windows path/TTY/CRLF handling, self-deletion guard.

**Out of scope:** VS Code `.devcontainer/devcontainer.json` (that's for interactive editing; this container is for autonomous agents), Docker-in-Docker, network egress filtering, auto-refresh of expired tokens.

---

## 2. The user flow

### First run (once per machine)

```bash
# 1. Clone SimpleHarness somewhere.
git clone https://github.com/<you>/SimpleHarness.git ~/SimpleHarness

# 2. cd to any git repo you want the agent to work on.
cd ~/projects/my-python-app     # or any TypeScript repo

# 3. Launch. First run builds the image (~3-5 min) and prints the login command.
~/SimpleHarness/scripts/launch.sh

# 4. Paste the login command it printed. Browser opens, you authenticate.
cd ~/SimpleHarness
docker compose run --rm --entrypoint claude simpleharness login

# 5. Re-run launch. This time it starts the watch loop.
cd ~/projects/my-python-app
~/SimpleHarness/scripts/launch.sh
```

### Steady state (every time after)

```bash
cd ~/projects/my-python-app
~/SimpleHarness/scripts/launch.sh
# Harness is now running. Edit your TASK.md in another window,
# walk away, come back when FINAL.md is written.
# Ctrl+C in the harness terminal for the correction prompt.
```

### Self-improvement mode (when project-leader should edit the toolbox)

```bash
~/SimpleHarness/scripts/launch.sh --allow-toolbox-edits
# Same as normal launch, but /opt/simpleharness is mounted RW
# so the project-leader role can edit roles/, workflows/, config.yaml.
```

### Clean slate

```bash
cd ~/SimpleHarness
docker compose down -v            # removes containers + named volumes
docker image rm simpleharness:latest
# Next launch rebuilds from scratch and requires `claude login` again.
```

---

## 3. Architecture at a glance

```
 Host (Windows 11 + Docker Desktop)
┌────────────────────────────────────────────────────────────────┐
│                                                                │
│  ~/SimpleHarness/          ~/projects/my-app/     Docker Desktop
│  (the toolbox)             (the worksite)        ┌───────────┐ │
│       │                         │                │ named vol │ │
│       │ bind :ro                │ bind :rw       │ simple-   │ │
│       │ (:rw with               │                │ harness-  │ │
│       │  --allow-                │                │ home      │ │
│       │  toolbox-                │                └─────┬─────┘ │
│       │  edits)                 │                      │       │
│       ▼                         ▼                      ▼       │
│  ┌───────────────────────────────────────────────────────────┐ │
│  │ Container: simpleharness:latest                           │ │
│  │   tini (PID 1) → entrypoint.sh → simpleharness watch      │ │
│  │                                                           │ │
│  │   /opt/simpleharness ◄── toolbox (read-only default)     │ │
│  │   /worksite          ◄── target repo (read-write)        │ │
│  │     ├── /worksite/.venv        (anon volume mask)        │ │
│  │     └── /worksite/node_modules (anon volume mask)        │ │
│  │   /home/harness      ◄── simpleharness-home (named)      │ │
│  │     ├── .claude/.credentials.json   (persists login)     │ │
│  │     ├── .claude.json                (global state)       │ │
│  │     ├── .local/bin/claude           (Claude Code CLI)    │ │
│  │     ├── .local/share/uv/tools/      (editable install)   │ │
│  │     └── .cache/uv                   (uv cache)           │ │
│  │                                                           │ │
│  │   user: harness (UID 1000)                                │ │
│  │   SIMPLEHARNESS_SANDBOX=1, SIMPLEHARNESS_WORKSITE=/worksite│ │
│  └───────────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────────┘
```

Single container, single image. No Docker socket, no SSH mount, no `~/.claude/` bind from host. Network egress is unrestricted (needed for Claude API, apt, npm, pip).

---

## 4. Files to add

Five files, all at the SimpleHarness repo root (or `scripts/` for the shell scripts). Total ≤225 lines.

### 4.1 `Dockerfile`

```dockerfile
# SimpleHarness sandbox image: Python 3.13 + uv + Node 20 + Claude Code CLI.
# The SimpleHarness repo is bind-mounted at /opt/simpleharness at runtime,
# the target worksite at /worksite. See compose.yml.
FROM python:3.13-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# OS deps. tini becomes PID 1 so SIGINT from `docker compose run` reaches
# the Python harness and drives the Ctrl+C → correction prompt flow.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git tini \
    && rm -rf /var/lib/apt/lists/*

# Node 20 LTS so agents can run npm/npx in TypeScript target repos.
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# uv installed system-wide so both root (build) and the harness user see it.
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

# Non-root user. UID 1000 = Docker Desktop's default mapping on Windows hosts.
RUN useradd -m -u 1000 -s /bin/bash harness
USER harness

# Native Claude Code installer. Must run from /tmp — at / it scans the whole
# filesystem and hangs in Docker. Binary lands at ~/.local/bin/claude.
WORKDIR /tmp
RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH="/home/harness/.local/bin:${PATH}"

WORKDIR /worksite
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/opt/simpleharness/scripts/entrypoint.sh"]
CMD ["simpleharness", "watch"]
```

**Why Python base, not Node:** the harness is Python. Node is needed only so the agent can run `npm` / `node` against TypeScript worksites — adding it costs two RUN layers. Starting from `node:lts` and bolting on uv + Python 3.13 is more lines and ships a full Node server runtime we don't use.

**Why install SimpleHarness at run-time, not build-time:** editable install against the bind-mounted toolbox means edits to `core.py`, `shell.py`, `roles/*.md`, and `workflows/*.md` apply without rebuilding the image. `core.py:177` resolves `toolbox_root()` from `Path(__file__).resolve().parent`, so the editable shim points at `/opt/simpleharness` with no extra env var.

### 4.2 `compose.yml`

```yaml
# SimpleHarness sandbox. Launch via scripts/launch.sh, which exports
# WORKSITE_PATH, COMPOSE_PROJECT_NAME, and TOOLBOX_MOUNT_RO before calling
# `docker compose run`.
#
# Upgrading the claude CLI baked into the image (after editing the Dockerfile):
#   cd ~/SimpleHarness
#   docker compose down -v    # MUST wipe the named volume — the RUN-layer
#                             # binary at ~/.local/bin/claude is shadowed
#                             # by the volume once it exists.
#   scripts/launch.sh

services:
  simpleharness:
    build:
      context: .
      dockerfile: Dockerfile
    image: simpleharness:latest
    # NO container_name: — launch.sh sets COMPOSE_PROJECT_NAME per worksite so
    # parallel instances against different repos do not collide on name.
    tty: true
    stdin_open: true
    working_dir: /worksite
    environment:
      SIMPLEHARNESS_SANDBOX: "1"
      SIMPLEHARNESS_WORKSITE: "/worksite"
      PYTHONDONTWRITEBYTECODE: "1"
      TERM: "xterm-256color"
      FORCE_COLOR: "1"
    volumes:
      # Toolbox: read-only by default. launch.sh --allow-toolbox-edits flips
      # TOOLBOX_MOUNT_RO=false for project-leader self-improvement sessions.
      - type: bind
        source: .
        target: /opt/simpleharness
        read_only: ${TOOLBOX_MOUNT_RO:-true}
      # Worksite: the user's target repo.
      - ${WORKSITE_PATH:?Run scripts/launch.sh — it exports WORKSITE_PATH}:/worksite
      # Anonymous volume masks: Linux can't use a Windows .venv or node_modules.
      - /worksite/.venv
      - /worksite/node_modules
      # Persistent harness home — holds claude credentials, the uv tool venv,
      # the uv cache, and the baked-in claude binary at ~/.local/bin/claude.
      - simpleharness-home:/home/harness

volumes:
  simpleharness-home:
```

### 4.3 `scripts/launch.sh`

```bash
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
    docker compose run --rm --entrypoint claude simpleharness login

[launch] Then re-run scripts/launch.sh.
EOF
  exit 1
fi

# --- launch ---
echo "[launch] worksite = ${WORKSITE_PATH}"
echo "[launch] project  = ${COMPOSE_PROJECT_NAME}"
exec docker compose run --rm simpleharness
```

### 4.4 `scripts/entrypoint.sh`

```bash
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
git config --global --add safe.directory '*'

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
```

### 4.5 `.dockerignore`

```gitignore
# Build context trimming. The Dockerfile does not COPY the toolbox in, so
# this only affects `docker compose build` context tar size.
.git/
.venv/
__pycache__/
**/__pycache__/
*.pyc
*.pyo
*.egg-info/
build/
dist/
.pytest_cache/
.ruff_cache/
node_modules/

# Editor / OS
.vscode/
.idea/
.DS_Store
Thumbs.db
```

### 4.6 `.gitattributes` — verify coverage

The repo already has `.gitattributes` (see recent commit `021a938`). Verify it normalizes `*.sh`, `Dockerfile`, `compose.yml`, and `*.py` to `eol=lf`. If it only has `* text=auto eol=lf`, that's already enough. Without this, CRLF line endings on `entrypoint.sh` or `launch.sh` from a Windows Git clone would break execution inside the Linux container.

---

## 5. Launch script flow

```
1. Parse --worksite PATH (default: cwd) and --allow-toolbox-edits flag.
2. Validate:
   a. Worksite has a .git/ directory.
   b. Worksite is NOT the SimpleHarness toolbox (realpath comparison).
   c. Neither path is a parent of the other.
3. Export MSYS_NO_PATHCONV=1.
4. Resolve absolute paths via cygpath -m (if Git Bash).
5. Export WORKSITE_PATH and COMPOSE_PROJECT_NAME=sh-<sha1-head-8 of WORKSITE_PATH>.
6. If --allow-toolbox-edits, export TOOLBOX_MOUNT_RO=false. Otherwise true.
7. cd into the toolbox so `docker compose` finds compose.yml.
8. Build image (cached no-op after first run).
9. Probe credentials volume: run a one-shot `sh -c '[ -f /home/harness/.claude/.credentials.json ]'`.
10. If credentials missing, print the exact `docker compose run --rm --entrypoint claude simpleharness login` one-liner and exit 1.
11. exec `docker compose run --rm simpleharness`  → tini → entrypoint.sh → simpleharness watch.
```

Two-command first-run is intentional: `claude login` opens a browser for OAuth, which can't be fully automated from a launcher script regardless of what we do.

---

## 6. Safety model

The container makes dangerous mode safe by shrinking the blast radius to exactly three things: the worksite bind mount, the named home volume, and outbound network. Everything else is either not mounted or mounted read-only.

### What the agent CAN reach

| Reach | Mount / mechanism | Risk |
|---|---|---|
| Worksite files | bind `:rw` | Can `rm -rf /worksite` → **deletes host files**. Use a git worktree or a throwaway clone for high-risk tasks. |
| Toolbox files | bind `:ro` default | None by default. `--allow-toolbox-edits` flips to `:rw` — only use for deliberate meta-work. |
| `/home/harness` | named volume `simpleharness-home` | Can only corrupt its own state. Recovery: `docker compose down -v`. |
| Network egress | Docker default bridge | Can exfiltrate code, hit internal services, call arbitrary APIs. Trade-off documented in [§13](#13-known-limitations-v01). |
| Claude API | via the above | Expected. Credentials live in the volume. |

### What the agent CANNOT reach

| Blocked thing | How | Why |
|---|---|---|
| Host Docker daemon | no `/var/run/docker.sock` mount | Single biggest safety win vs Paperclip. No sibling containers, no DinD breakout. |
| SSH keys | no `~/.ssh` mount | Agent has no git push rights. User pushes from host. |
| Host git config | no `~/.gitconfig` mount | Container sets its own via `entrypoint.sh`. |
| Host `~/.claude/` | never bind-mounted from host | Prevents host `claude` and container `claude` from racing on `.credentials.json`. Named volume only. |
| GitHub CLI auth | `gh` not installed in image | Same reasoning as SSH. |
| Anywhere outside `/worksite` and `/home/harness` for writes | read-only rootfs layers | `:ro` toolbox + ephemeral rootfs. |

### Self-deletion guard

`launch.sh` refuses to start if `realpath(worksite)` equals or overlaps `realpath(toolbox)`. Without this, running `scripts/launch.sh` from inside the SimpleHarness repo itself would bind-mount the toolbox at `/worksite`, and one `rm -rf` in dangerous mode would delete the harness.

### Dangerous mode activation is per-worksite

The toolbox `config.yaml` ships with `permissions.mode: safe`. The entrypoint writes `permissions.mode: dangerous` only into `<worksite>/simpleharness/config.yaml`, and only if the user has not provided one themselves. Host-side `simpleharness watch` still refuses dangerous mode because (a) the toolbox default is `safe`, and (b) `shell.py:699-709` requires a sandbox marker the host doesn't have.

### How the harness knows it's sandboxed

`shell.py:699-709`:
```python
in_sandbox = (
    Path("/.dockerenv").exists()
    or os.environ.get("SIMPLEHARNESS_SANDBOX") == "1"
)
```

Belt and braces: `/.dockerenv` is created by Docker on every container start, and compose sets `SIMPLEHARNESS_SANDBOX=1` as an env var. Either alone would suffice.

The harness also accepts a `--i-know-its-dangerous` CLI flag that overrides the sandbox check, for cases where the user deliberately wants dangerous mode on an unsandboxed host.

---

## 7. Persistence and first-run auth

### What persists across container restarts

| File | Location | Persists because |
|---|---|---|
| Claude login token | `/home/harness/.claude/.credentials.json` | `simpleharness-home` named volume |
| Claude global state | `/home/harness/.claude.json` | same volume |
| Claude CLI binary | `/home/harness/.local/bin/claude` | same volume (baked at build, copied on volume create) |
| SimpleHarness editable install | `/home/harness/.local/share/uv/tools/simpleharness/` | same volume |
| uv cache | `/home/harness/.cache/uv` | same volume |
| Task state, logs, FINAL.md | `<worksite>/simpleharness/` | worksite bind mount (visible on host immediately) |

### First-run login

Two commands, intentionally split:

```bash
~/SimpleHarness/scripts/launch.sh
# [launch] Claude Code credentials not found in the persistent volume.
# [launch] Run this once to log in:
#     cd "/c/Users/you/SimpleHarness"
#     docker compose run --rm --entrypoint claude simpleharness login

# Paste the login command:
cd ~/SimpleHarness
docker compose run --rm --entrypoint claude simpleharness login
# (browser opens, you authenticate, token written to the volume, exit 0)

# Re-run launch:
cd ~/projects/my-app
~/SimpleHarness/scripts/launch.sh
# Now it proceeds past the credentials probe and starts watch.
```

### Token expiry recovery

When `claude` starts returning auth errors inside the container, the user re-runs exactly the login command above. The token file is overwritten in place in the named volume. No other action needed.

### Credentials-in-named-volume shadowing trap

A baked-in binary at `~/.local/bin/claude` is copied into the volume **only on first volume creation**. If you later edit the Dockerfile to upgrade the `claude` version and `docker compose build`, the existing volume keeps the old binary. The `compose.yml` comment lists the fix: `docker compose down -v` before rebuilding. See [§13](#13-known-limitations-v01).

---

## 8. Python and TypeScript support

### Preinstalled in the image

| Tool | Version | For |
|---|---|---|
| Python | 3.13 (from `python:3.13-slim-bookworm`) | the harness + agents running pytest, ruff, etc. |
| uv | latest from astral.sh installer | the harness's `uv run` / `uv sync` / `uv tool` usage |
| Node | 20 LTS from NodeSource | agents running `npm`, `npx`, `node` in TS worksites |
| git | Debian default | commits, branches, diffs |
| claude | latest from `claude.ai/install.sh` | the Claude Code CLI |
| tini | Debian default | PID 1 signal forwarding |

### Cross-platform `.venv` / `node_modules` masking

The bind mount exposes the host worksite's tree, which may already contain a Windows `.venv/` or `node_modules/`. Linux cannot use them. `compose.yml` declares anonymous volumes at `/worksite/.venv` and `/worksite/node_modules` that mask whatever the host has there:

```yaml
volumes:
  - /worksite/.venv
  - /worksite/node_modules
```

First use in each worksite:
- **Python**: the agent runs `uv sync` (or the worksite's equivalent) which populates the anonymous `.venv` with Linux-native packages. Persists until `docker compose down -v`.
- **TypeScript**: the agent runs `npm ci` (or `pnpm install`) which populates the anonymous `node_modules` with Linux-native bindings.

The host's `.venv/` and `node_modules/` are untouched — the mask hides them from the container, not the host.

---

## 9. Multi-worksite parallelism

`intent.md:31` allows multiple SimpleHarness instances against different worksites simultaneously. The launcher supports this via per-worksite compose project names:

```bash
HASH=$(printf '%s' "${WORKSITE_PATH}" | sha1sum | cut -c1-8)
export COMPOSE_PROJECT_NAME="sh-${HASH}"
```

Two different worksites → two different project names → two independent containers + two independent `_default` networks + two independent `simpleharness-home` volumes (each scoped to its own project).

| Resource | Shared across worksites? |
|---|---|
| Image (`simpleharness:latest`) | Yes — built once, cached. |
| Named home volume | **No** — each COMPOSE_PROJECT_NAME gets its own `sh-<hash>_simpleharness-home`. Each worksite needs its own `claude login` the first time. |
| Container | No — different project names → different generated container names. |
| Worksite bind mount | No — each project mounts its own worksite. |

If the user wants to share one credentials volume across all worksites, they can hand-edit `compose.yml` to give the volume a global external name. Not the default — per-project isolation is safer.

---

## 10. Windows / Git Bash caveats

| Risk | Mitigation |
|---|---|
| CRLF line endings on `*.sh`, `Dockerfile`, `compose.yml` break execution in Linux | `.gitattributes` with `* text=auto eol=lf` (already present in repo — verify it covers these extensions) |
| Git Bash mangles `/opt/simpleharness` → `C:/Program Files/Git/opt/simpleharness` when passing to docker | `export MSYS_NO_PATHCONV=1` at the top of `launch.sh` |
| `WORKSITE_PATH` needs to be Windows-native for Docker Desktop | `cygpath -m "${WORKSITE_REAL}"` converts `/c/Users/...` → `C:/Users/...` |
| `docker compose run -it` with mintty: signals may not propagate | tini as PID 1 handles SIGINT forwarding correctly in modern Docker Desktop. If Ctrl+C still doesn't work, fallback is `winpty docker compose run --rm simpleharness` |
| Paths with spaces in `WORKSITE_PATH` | All `"${var}"` references in `launch.sh` are double-quoted |
| Host UID ≠ 1000 | Docker Desktop's virtiofs/9P translation makes this transparent on Windows. No gosu remap needed. |
| `git status` inside container sees every file as modified | `entrypoint.sh` sets `git config --global core.autocrlf input` |
| `git` refuses to operate on bind-mounted repo due to ownership mismatch | `entrypoint.sh` sets `git config --global --add safe.directory '*'` |

If you hit a problem not in this table, run `docker compose run --rm --entrypoint bash simpleharness` for an interactive shell inside the container and investigate.

---

## 11. Cleanup and teardown

```bash
# Stop whatever is running for the current worksite
cd ~/SimpleHarness
docker compose stop

# Remove the container (keeps the named volume → login persists)
docker compose rm -f

# Wipe everything for this worksite — containers + named volume + anon volumes
docker compose down -v
# Next launch requires `claude login` again.

# Nuke the image (force rebuild from scratch)
docker image rm simpleharness:latest

# Nuke EVERY SimpleHarness project (if you have multiple worksites)
docker volume ls | awk '/sh-[0-9a-f]+_simpleharness-home/ {print $2}' | xargs -r docker volume rm
```

### When to wipe the volume

- **After editing the Dockerfile** (especially if you changed the `claude` install step). The old binary at `~/.local/bin/claude` is shadowed by the existing volume. Always: `docker compose down -v` → `scripts/launch.sh`.
- **When switching to a different `claude login` account.** Otherwise the old token persists.
- **When the harness venv at `~/.local/share/uv/tools/simpleharness/` gets into a weird state.** Nuking the volume forces a clean `uv tool install -e` on the next entrypoint run.

---

## 12. Handling Claude Code CLI errors

> **Status: proposed design.** The retry fields (`retry_count`, `retry_after`) and the classifier function described below are not yet implemented in the harness. This section specifies the intended behavior for implementation in a future task.

The harness runs unattended in the container. Transient CLI failures — usage limits, server overload, network blips — must not tear down the flow. Permanent failures must stop the task cleanly without burning retries. The proposed policy is **two new optional fields on `STATE.md`** plus **one classifier function** that inspects every failed `claude -p` session.

### Two new STATE.md fields

```yaml
---
status: active | blocked | done
phase: <role>
next_role: <role>
blocked_reason: <string>
retry_count: <int>            # bumps on transient failures; cleared on success
retry_after: <ISO timestamp>  # watch loop skips the task while now < retry_after
---
```

Both are optional. Present only while a task is in backoff or parked.

### Three outcomes from a failed session

| Outcome | Trigger | Set in STATE | Watch-loop effect |
|---|---|---|---|
| **Usage limit (reset known)** | Error names a reset time (hourly/daily quota hit) | `retry_after = <reset>` | Task parked until reset. `retry_count` is **not** bumped — quota waits aren't the agent's fault. |
| **Transient** | 529 Overloaded, 503, rate-limit without a reset time, network/DNS/timeout | `retry_count += 1`, `retry_after = now + backoff[retry_count - 1]` | Parked briefly, resumed next tick. On `retry_count == 5`, escalate to `fatal`. |
| **Fatal** | Auth expired (401), invalid model, corrupted state, anything non-zero without a parseable transient signal | `status = blocked`, `blocked_reason = <last stderr line>`, counters cleared | Task stops. User reads logs and intervenes. |

**Backoff is a fixed list:** `[30, 60, 120, 240, 300]` seconds. Five hardcoded values, no exponential math, no jitter. Simpler than any formula and easier to reason about when tailing logs.

### Detection

Two inputs, both already available to the harness:

1. **Exit code** from `spawn_claude()` at `session.py:54-73`.
2. **Stream-json error events** in the per-session `.jsonl` log the harness already writes (`{"type":"error", ...}`).

A tiny pattern table maps recognized signals to an outcome. Rough shape:

| Signal (case-insensitive, in stream-json error body or last stderr line) | Outcome |
|---|---|
| `usage limit.*reset.*<ISO>` (regex captures the timestamp) | `usage_limit` with captured reset |
| `overloaded`, `\b529\b`, `\b503\b`, `rate.?limit`, `ECONNRESET`, `ETIMEDOUT`, `DNS`, `timeout` | `transient` |
| `\b401\b`, `invalid api key`, `not authenticated`, `token expired` | `fatal` with reason `auth_expired — run claude login in container` |
| Anything else, non-zero exit | `fatal` with the last stderr line as reason |

Unknown signals default to `fatal` rather than `transient` — loud-stop beats silent-retry-forever.

### Watch-loop change

In `tick_once()`, **before** selecting a task to run:

```
for each active task:
    if STATE.retry_after is set and now < STATE.retry_after:
        skip this task this tick
        (print once per transition: "[harness] parked <slug> until <retry_after>")
```

**After** a session exits:

- **Success** → clear `retry_count` and `retry_after`, advance the role normally.
- **`usage_limit`** → write `retry_after=<reset>`, leave `status=active`, do not advance.
- **`transient`** → bump `retry_count`, write `retry_after=now+backoff[retry_count-1]`, leave `status=active`, do not advance. On `retry_count == 5`, convert to `fatal` (reason `transient_exhausted: <signal>`).
- **`fatal`** → set `status=blocked`, write `blocked_reason`, clear counters.

User intervention via `Ctrl+C` → `CORRECTION.md` implicitly clears the wait: the next tick consumes the correction regardless of `retry_after`, because the intervention path is higher-priority than the backoff gate.

### Why this stays simple

- **Two fields, no new file, no new process.** Backoff state lives in the same `STATE.md` the harness already owns.
- **Restart-resilient by default.** Container stop/start mid-backoff → next tick re-reads `STATE.md`, sees `retry_after` in the future, waits. No in-memory timers to persist, no cron jobs.
- **Fixed backoff list.** Five numbers. No formulas to tune, no tests to write beyond "does index lookup work".
- **One classifier function** with one pattern table. Anything unrecognized becomes `fatal` — failure is the safe default.
- **Parallel tasks don't interfere.** Each task has its own `STATE.md` → its own `retry_after`. Waiting on task A doesn't block task B.
- **Multi-instance safe.** Two containers against two worksites have two separate STATE files. Zero coordination needed.

### What the user sees in the container log

```
[harness] session failed: usage_limit — parked 003-refactor-auth until 2026-04-08T15:30:00Z
[harness] idle sleep 30s ...
[harness] retry_after passed — resuming 003-refactor-auth
```

```
[harness] session failed: transient (retry 2/5) — parked 003-refactor-auth until 2026-04-08T14:47:12Z
```

```
[harness] session failed: fatal — blocked 003-refactor-auth: auth_expired — run claude login in container
```

Log messages name the task slug so parallel instances tailing logs stay readable.

### Out of scope for v0.1

- Automatic re-login on `auth_expired` (browser OAuth has no headless path today).
- Per-model or per-endpoint rate-limit differentiation — one `transient` bucket handles all.
- Retry budgeting across tasks (each task has its own independent counter).
- Circuit-breaker across the whole worksite (e.g. "5 tasks failed in a row → stop everything"). Add later if it becomes a real problem.

---

## 13. Known limitations (v0.1)

| # | Limitation | Why it's deferred | Workaround |
|---|---|---|---|
| 1 | **No network egress filtering.** The container can reach anywhere the host can. | Agents need the Claude API, apt, pip, npm, and likely your target repo's test fixtures. A proper egress allowlist is a separate design exercise. | Review agent behavior; don't run against repos with secrets in the working tree. |
| 2 | **Windows host only.** Linux host support needs UID/GID remap via gosu (Paperclip pattern). | Out of scope for v0.1; adds ~30 lines of entrypoint complexity. | Linux users: add gosu + UID remap per Paperclip's `docker-entrypoint.sh`. |
| 3 | **Stale `claude` binary after image rebuild.** Named-volume shadowing trap. | True docker limitation, not fixable without a different persistence strategy. | Document is explicit: `docker compose down -v` before rebuilding. |
| 4 | **Manual token expiry recovery.** Harness doesn't detect 401 from `claude -p`. | Requires a `shell.py` / `session.py` change to parse exit codes or stream-json error events. | User reruns the login one-liner when they notice agent failures. |
| 5 | **Package lockfile drift between host and container.** `npm install` inside the container may write a different lockfile than the host's Windows `npm install`. | Known cross-platform issue unrelated to SimpleHarness. | Commit only from inside the container, or add `npm ci --ignore-scripts` guidance to role files. |
| 6 | **Self-deletion risk if worksite is bind-mounted `:rw` to a volatile path.** Launcher protects against `WORKSITE == TOOLBOX`, but not against `WORKSITE = /c/Users/.../Documents`. | By design — user knows their repos. | Point launcher at a specific repo directory, not your home folder. Consider using `git worktree` for high-stakes tasks. |
| 7 | **Whole-home named volume conflates user state and tool binaries.** One wipe nukes everything. | Simpler than multiple narrowly-scoped volumes, and matches Paperclip's working pattern. | Documented — user accepts the trade-off. |
| 8 | **Only tested with Git Bash + Docker Desktop on Windows 11.** PowerShell and Windows Terminal may need different TTY handling. | First-cut target is the user's own setup. | If a user reports it, document the fix in [§10](#10-windows--git-bash-caveats). |

---

## 14. Future extensions

- **Linux host support.** Add a gosu-based entrypoint that remaps the `harness` user to `$(id -u)`:`$(id -g)` from env vars passed by launch.sh, mirroring Paperclip's `docker-entrypoint.sh`.
- **Egress allowlist.** `network_mode: bridge` + iptables rules restricting outbound to `api.anthropic.com`, `deb.debian.org`, `registry.npmjs.org`, and the target repo's known remotes. Useful for air-gapped work.
- **Auto-refresh of expired tokens.** When Claude Code ships a device-code OAuth flow, the launcher can automate login instead of printing the manual command.
- **VS Code `.devcontainer/` alias.** For users who want interactive editing inside the same image, add a `.devcontainer/devcontainer.json` that reuses `compose.yml` but swaps `CMD` for `sleep infinity`.
- **Per-worksite entrypoint hook.** If `<worksite>/simpleharness/pre-watch.sh` exists, the entrypoint runs it before `exec simpleharness watch`. Lets a worksite install its own dependencies or start a local service before the harness starts.
- **`simpleharness doctor --container` mode.** Add container-specific checks: credentials volume populated, toolbox mount mode, image age vs. Dockerfile mtime.

---

## Reference map

- `core.py:177` — `toolbox_root()` resolution via `Path(__file__).resolve().parent`.
- `shell.py:88-93` — `worksite_root()` precedence: `--worksite` > `SIMPLEHARNESS_WORKSITE` > cwd.
- `core.py:714-761` — `build_claude_cmd()` — where the permission flags are chosen.
- `session.py:54-73` — `spawn_claude()` — subprocess setup (no PTY; only parent needs TTY).
- `shell.py:699-709` — sandbox check driving dangerous-mode gating.
- `config.yaml:35-48` — permissions block, shipped default `mode: safe`.
- `intent.md:26-31` — two-repo split and multi-worksite parallelism.
- `intent.md:73-86` — intervention model (Ctrl+C → correction prompt).
- `intent.md:108-112` — dangerous mode and sandbox gating requirement.
- `intent.md:115-119` — project-leader toolbox-edit privilege (the reason `--allow-toolbox-edits` exists).
