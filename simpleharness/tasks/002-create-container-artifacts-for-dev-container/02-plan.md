# 02 — Plan (plan-writer)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create 5 Docker container artifacts from the spec at `docs/dev-container.md` §4.1–4.5 so SimpleHarness can run with bypass permissions safely inside a sandboxed container.

**Architecture:** Transcribe the spec's complete code blocks verbatim (brainstorm Approach B). The spec was validated against the current codebase — all 6 line references match, no deviations needed. Validate with shellcheck + `docker compose config` + `docker compose build` before committing.

**Tech Stack:** Docker, Docker Compose, Bash (shellcheck), Python 3.13, Node 20, uv, tini

---

## Context

Task 002 creates the container infrastructure for SimpleHarness's "dangerous mode" sandbox. The upstream spec (`docs/dev-container.md`, refined by task 001) contains complete file contents for all 5 deliverables in §4.1–4.5.

**Worksite state:**
- No Docker infrastructure exists yet (no Dockerfile, compose.yml, .dockerignore)
- `scripts/` directory exists (contains `check_fp_purity.py`)
- `.gitattributes` has `* text=auto eol=lf` (covers all new files)
- Branch: `feature/dev-container`

**Brainstorm outcome:** All spec line references validated. Zero deviations from spec identified. Recommended approach: transcribe verbatim, validate with shellcheck and compose config.

## Approach

Transcribe §4.1–4.5 code blocks from `docs/dev-container.md` in dependency order. Each file is created, then validated against its acceptance criteria before moving on. Shell scripts get shellcheck validation. The full pipeline is tested with `docker compose build`. No code design decisions remain — the spec is implementation-ready.

**Why this order:** `.dockerignore` first (affects build context), then `Dockerfile` (foundational), then `compose.yml` (references Dockerfile), then `entrypoint.sh` and `launch.sh` (referenced by compose/Dockerfile at runtime via bind mount). Validation steps interleave after each script creation.

## Steps

### Task 1: Pre-flight checks

- [ ] **Step 1.1: Verify Docker Desktop is running**

  Run: `docker info` (should succeed without error)

  Acceptance: Docker daemon responds. If not, stop — nothing else works.

- [ ] **Step 1.2: Verify shellcheck is available**

  Run: `shellcheck --version`

  Acceptance: shellcheck is installed and reports a version. If missing, install it before proceeding (`apt-get install shellcheck` or equivalent).

- [ ] **Step 1.3: Verify scripts/ directory exists**

  Run: `ls scripts/`

  Acceptance: Directory exists (should contain `check_fp_purity.py`).

### Task 2: Create `.dockerignore`

**Files:**
- Create: `.dockerignore`
- Reference: `docs/dev-container.md` §4.5

- [ ] **Step 2.1: Create `.dockerignore` from §4.5**

  Copy the code block from `docs/dev-container.md` §4.5 verbatim into `.dockerignore` at the repo root.

  Acceptance: File exists at repo root. Contains exclusions for `.git/`, `.venv/`, `__pycache__/`, `*.pyc`, `*.egg-info/`, `build/`, `dist/`, `.pytest_cache/`, `.ruff_cache/`, `node_modules/`, editor/OS files.

- [ ] **Step 2.2: Commit**

  ```
  git add .dockerignore
  git commit -m "feat(container): add .dockerignore for build context trimming (task 002)"
  ```

### Task 3: Create `Dockerfile`

**Files:**
- Create: `Dockerfile`
- Reference: `docs/dev-container.md` §4.1

- [ ] **Step 3.1: Create `Dockerfile` from §4.1**

  Copy the code block from `docs/dev-container.md` §4.1 verbatim into `Dockerfile` at the repo root.

  Acceptance: File exists. Key properties:
  - Base image: `python:3.13-slim-bookworm`
  - Installs: ca-certificates, curl, git, tini, Node 20 LTS, uv
  - Creates non-root user `harness` (UID 1000)
  - Installs Claude Code CLI from `/tmp` (not `/`)
  - Sets `tini` as ENTRYPOINT with `/opt/simpleharness/scripts/entrypoint.sh`
  - Working directory: `/worksite`

- [ ] **Step 3.2: Commit**

  ```
  git add Dockerfile
  git commit -m "feat(container): add Dockerfile with Python 3.13 + Node 20 + Claude CLI (task 002)"
  ```

### Task 4: Create `compose.yml`

**Files:**
- Create: `compose.yml`
- Reference: `docs/dev-container.md` §4.2

- [ ] **Step 4.1: Create `compose.yml` from §4.2**

  Copy the code block from `docs/dev-container.md` §4.2 verbatim into `compose.yml` at the repo root.

  Acceptance: File exists. Key properties:
  - Service `simpleharness` with `build: context: .`
  - `SIMPLEHARNESS_SANDBOX: "1"` and `SIMPLEHARNESS_WORKSITE: "/worksite"` in environment
  - Toolbox bind mount (`.` → `/opt/simpleharness`, read_only controlled by `${TOOLBOX_MOUNT_RO:-true}`)
  - Worksite bind mount (`${WORKSITE_PATH}` → `/worksite`)
  - Anonymous volume masks for `/worksite/.venv` and `/worksite/node_modules`
  - Named volume `simpleharness-home` → `/home/harness`
  - `tty: true`, `stdin_open: true`

- [ ] **Step 4.2: Validate compose syntax**

  Run: `WORKSITE_PATH=/tmp docker compose config`

  Acceptance: Valid YAML output, no errors. Note: `/tmp` is a dummy path — this validates YAML syntax and variable interpolation, not that the worksite is a valid git repo.

- [ ] **Step 4.3: Commit**

  ```
  git add compose.yml
  git commit -m "feat(container): add compose.yml with sandbox env and volume mounts (task 002)"
  ```

### Task 5: Create `scripts/entrypoint.sh`

**Files:**
- Create: `scripts/entrypoint.sh`
- Reference: `docs/dev-container.md` §4.4

- [ ] **Step 5.1: Create `scripts/entrypoint.sh` from §4.4**

  Copy the code block from `docs/dev-container.md` §4.4 verbatim into `scripts/entrypoint.sh`.

  Acceptance: File exists. Key behaviors:
  - Sets `PATH` to include `/home/harness/.local/bin`
  - Configures git (autocrlf=input, safe.directory=*)
  - Installs simpleharness via `uv tool install -e /opt/simpleharness` if not already installed
  - Runs `simpleharness init --worksite /worksite` if `/worksite/simpleharness` doesn't exist
  - Writes `permissions.mode: dangerous` to worksite config if not present
  - Prints version banner
  - `exec "$@"` to hand off to CMD

- [ ] **Step 5.2: Make executable**

  Run: `chmod +x scripts/entrypoint.sh`

- [ ] **Step 5.3: Run shellcheck**

  Run: `shellcheck scripts/entrypoint.sh`

  Acceptance: Zero errors, zero warnings. If shellcheck flags issues, fix them inline (pre-authorized by TASK.md autonomy: "shell script implementation details").

- [ ] **Step 5.4: Commit**

  ```
  git add scripts/entrypoint.sh
  git commit -m "feat(container): add entrypoint.sh bootstrap script (task 002)"
  ```

### Task 6: Create `scripts/launch.sh`

**Files:**
- Create: `scripts/launch.sh`
- Reference: `docs/dev-container.md` §4.3

- [ ] **Step 6.1: Create `scripts/launch.sh` from §4.3**

  Copy the code block from `docs/dev-container.md` §4.3 verbatim into `scripts/launch.sh`.

  Acceptance: File exists. Key behaviors:
  - Parses `--worksite`, `--allow-toolbox-edits`, `-h`/`--help`
  - Validates worksite is a git repo (`.git/` check)
  - Self-deletion guard: refuses if worksite overlaps toolbox (realpath comparison, both directions)
  - Exports `MSYS_NO_PATHCONV=1` for Git Bash
  - Uses `cygpath -m` if available for Windows path normalization
  - Computes per-worksite `COMPOSE_PROJECT_NAME=sh-<sha1-head-8>`
  - Exports `TOOLBOX_MOUNT_RO` based on `--allow-toolbox-edits`
  - `cd`s to toolbox dir so compose finds `compose.yml`
  - Runs `docker compose build`
  - Probes credentials volume (`/home/harness/.claude/.credentials.json`)
  - Prints login instructions if credentials missing, exits 1
  - `exec docker compose run --rm simpleharness`

- [ ] **Step 6.2: Make executable**

  Run: `chmod +x scripts/launch.sh`

- [ ] **Step 6.3: Run shellcheck**

  Run: `shellcheck scripts/launch.sh`

  Acceptance: Zero errors, zero warnings. If shellcheck flags issues, fix them inline (pre-authorized).

- [ ] **Step 6.4: Commit**

  ```
  git add scripts/launch.sh
  git commit -m "feat(container): add launch.sh host-side launcher (task 002)"
  ```

### Task 7: Verify `.gitattributes` coverage

**Files:**
- Read-only: `.gitattributes`
- Reference: `docs/dev-container.md` §4.6

- [ ] **Step 7.1: Confirm LF normalization**

  Read `.gitattributes` and confirm it contains `* text=auto eol=lf`.

  Acceptance: The wildcard `*` pattern with `eol=lf` covers all new files (`*.sh`, `Dockerfile`, `compose.yml`, `.dockerignore`) without needing per-extension rules. No modification needed.

### Task 8: Integration validation

- [ ] **Step 8.1: Build the Docker image**

  Run: `docker compose build`

  Acceptance: Build completes successfully. Image tagged `simpleharness:latest`. Python 3.13, Node 20, uv, and Claude CLI are installed in the image.

- [ ] **Step 8.2: Verify sandbox environment inside container**

  Run a one-shot container to verify the sandbox marker:

  ```
  WORKSITE_PATH=/tmp docker compose run --rm --entrypoint sh simpleharness -c 'echo SANDBOX=$SIMPLEHARNESS_SANDBOX'
  ```

  Acceptance: Output includes `SANDBOX=1`. This confirms the environment variable that satisfies `shell.py:699-709` is set correctly.

- [ ] **Step 8.3: Verify installed tools inside container**

  Run a one-shot container:

  ```
  WORKSITE_PATH=/tmp docker compose run --rm --entrypoint sh simpleharness -c 'python3 --version && node --version && uv --version && claude --version'
  ```

  Acceptance: All four commands succeed. Python reports 3.13.x, Node reports v20.x.

### Task 9: Final commit and state update

- [ ] **Step 9.1: Final shellcheck on both scripts (paranoia check)**

  Run: `shellcheck scripts/launch.sh scripts/entrypoint.sh`

  Acceptance: Zero errors across both files.

- [ ] **Step 9.2: Update STATE.md**

  Set `phase=plan`, `next_role=developer`, `status=active`.

## Files to touch

| Action | Path | Source |
|--------|------|--------|
| Create | `.dockerignore` | §4.5 |
| Create | `Dockerfile` | §4.1 |
| Create | `compose.yml` | §4.2 |
| Create | `scripts/entrypoint.sh` | §4.4 |
| Create | `scripts/launch.sh` | §4.3 |
| Verify | `.gitattributes` | §4.6 (no changes expected) |
| Edit | `simpleharness/tasks/002-…/STATE.md` | phase/next_role update |

## Risks

1. **shellcheck flags spec code** — The spec scripts haven't been shellchecked yet. Mitigation: fix inline; TASK.md pre-authorizes shell script implementation details. Likelihood: low (spec was carefully written).

2. **Named-volume shadowing** — The Claude CLI binary baked at build is shadowed by the persistent home volume on subsequent runs. Mitigation: documented in `compose.yml` inline comments per spec. Recovery: `docker compose down -v` before rebuilding.

3. **Windows path handling** — `cygpath` and `MSYS_NO_PATHCONV` in `launch.sh` can only be fully tested on the actual Windows + Git Bash host. Mitigation: shellcheck validates script syntax; full integration testing is the developer's responsibility on the host.

4. **Claude Code installer instability** — `curl -fsSL https://claude.ai/install.sh | bash` is an external dependency. If the URL or installer behavior changes, the Dockerfile build breaks. Mitigation: low risk for v0.1; the URL is the official Claude install path.

5. **Docker Desktop not running or misconfigured** — Steps 8.1–8.3 require Docker Desktop. Mitigation: pre-flight check in Step 1.1. If Docker isn't available, the developer can still create and shellcheck all files, deferring build validation.

## Verification

The work is done when ALL of these pass:

```
shellcheck scripts/launch.sh scripts/entrypoint.sh   → 0 errors
WORKSITE_PATH=/tmp docker compose config              → valid YAML, no errors
docker compose build                                  → success, image tagged
WORKSITE_PATH=/tmp docker compose run --rm \
  --entrypoint sh simpleharness \
  -c 'echo $SIMPLEHARNESS_SANDBOX'                    → prints "1"
cat .gitattributes                                    → contains "* text=auto eol=lf"
ls Dockerfile compose.yml .dockerignore \
   scripts/launch.sh scripts/entrypoint.sh            → all 5 files exist
```

## Subagents dispatched (this session)

| Model | Task | Key finding |
|-------|------|-------------|
| Haiku | Read TASK.md, 01-brainstorm.md, STATE.md | Full contents retrieved; brainstorm recommends Approach B (transcribe + validate) |
| Haiku | Explore worksite structure | No Docker files exist; `scripts/` has `check_fp_purity.py`; `.gitattributes` has wildcard LF rule |
| Haiku | Read full `docs/dev-container.md` | Complete spec with §4.1–4.5 code blocks returned |
| Sonnet | Sanity-check draft plan against TASK.md | Identified: add Docker pre-check, sandbox verification step, tighten gitattributes criterion, note compose config limitation |
