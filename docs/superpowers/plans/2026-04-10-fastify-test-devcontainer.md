# Fastify-Test Dev Container Setup — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire SimpleHarness's existing dev container to run against `fastify-test` with approver-mode permissions.

**Architecture:** Pre-create the worksite scaffold and config.yaml on the host so that `entrypoint.sh` skips its default dangerous-mode write. Then launch via `scripts/launch.sh --worksite` and verify the watch loop starts in approver mode.

**Tech Stack:** Bash, Docker Compose, YAML config

**Worksite path:** `C:\Users\OleJohanBondahl\Documents\Github_zen\fastify-test`
**Toolbox path:** `C:\Users\OleJohanBondahl\Documents\Github_OJ\SimpleHarness`

---

### Task 1: Create worksite scaffold and config

**Files:**
- Create: `<fastify-test>/simpleharness/config.yaml`
- Create: `<fastify-test>/simpleharness/tasks/` (directory)
- Create: `<fastify-test>/simpleharness/memory/WORKSITE.md`
- Create: `<fastify-test>/simpleharness/logs/` (directory)

- [ ] **Step 1: Create the simpleharness directory structure**

```bash
mkdir -p C:/Users/OleJohanBondahl/Documents/Github_zen/fastify-test/simpleharness/tasks
mkdir -p C:/Users/OleJohanBondahl/Documents/Github_zen/fastify-test/simpleharness/memory
mkdir -p C:/Users/OleJohanBondahl/Documents/Github_zen/fastify-test/simpleharness/logs
```

- [ ] **Step 2: Write the per-worksite config with approver mode**

Create `C:\Users\OleJohanBondahl\Documents\Github_zen\fastify-test\simpleharness\config.yaml`:

```yaml
# Per-worksite SimpleHarness config for fastify-test.
# Approver mode: Sonnet reviews any command outside the static allowlist.
# Extra bash patterns extend the default allowlist with Node/TS tooling.
permissions:
  mode: approver
  approver_model: sonnet
  extra_bash_allow:
    - "npm *"
    - "npx *"
    - "vitest *"
    - "tsc *"
```

- [ ] **Step 3: Write the worksite memory seed file**

Create `C:\Users\OleJohanBondahl\Documents\Github_zen\fastify-test\simpleharness\memory\WORKSITE.md`:

```markdown
# Worksite memory

Long-term notes that every session can read.
```

- [ ] **Step 4: Verify the scaffold matches what `simpleharness init` would create**

```bash
ls -R C:/Users/OleJohanBondahl/Documents/Github_zen/fastify-test/simpleharness/
```

Expected output:
```
config.yaml  logs/  memory/  tasks/

logs:

memory:
WORKSITE.md

tasks:
```

- [ ] **Step 5: Commit the scaffold in the fastify-test repo**

```bash
cd C:/Users/OleJohanBondahl/Documents/Github_zen/fastify-test
git add simpleharness/config.yaml simpleharness/memory/WORKSITE.md
git commit -m "chore: scaffold simpleharness worksite with approver-mode config"
```

Note: `logs/` and `tasks/` are empty dirs — git doesn't track empty dirs. They'll be recreated by `simpleharness init` if missing, or we can add `.gitkeep` files if needed.

---

### Task 2: Verify container launch

- [ ] **Step 1: Build the Docker image**

From the SimpleHarness (toolbox) directory:

```bash
cd C:/Users/OleJohanBondahl/Documents/Github_OJ/SimpleHarness
WORKSITE_PATH=$(cygpath -m C:/Users/OleJohanBondahl/Documents/Github_zen/fastify-test) \
  docker compose build
```

- [ ] **Step 2: Launch the container with fastify-test as worksite**

```bash
bash scripts/launch.sh --worksite /c/Users/OleJohanBondahl/Documents/Github_zen/fastify-test
```

Expected: entrypoint.sh should:
- Install simpleharness (first run)
- Skip init (simpleharness/ dir already exists)
- Skip config.yaml write (file already exists)
- Print version info
- Start `simpleharness watch`

Watch should report "no active tasks" and idle (since tasks/ is empty).

- [ ] **Step 3: Verify approver mode is active**

Inside the container (or from watch output), confirm:
- `permissions.mode=approver` is reported
- No sandbox refusal (approver mode doesn't require sandbox marker, only dangerous does)

- [ ] **Step 4: Stop the container**

Ctrl+C to stop the watch loop, or `docker compose down` from another terminal.

---

### Task 3: Smoke-test with a throwaway task

- [ ] **Step 1: Create a minimal test task**

Create `C:\Users\OleJohanBondahl\Documents\Github_zen\fastify-test\simpleharness\tasks\001-smoke-test\TASK.md`:

```markdown
---
title: "Smoke test — list project files"
workflow: universal
worksite: .
---

# Goal

List the top-level files in the worksite and write a one-paragraph summary of what this project contains to `simpleharness/tasks/001-smoke-test/SUMMARY.md`.

## Success criteria

- [ ] SUMMARY.md exists and contains a description of the project
- [ ] No errors in the session log

## Boundaries

- Read-only exploration. Do not modify any project files.
- Only create SUMMARY.md inside this task's directory.

## Autonomy

- Pre-authorized: reading any file, running `ls`, `cat`, `git log`
- Must block: any file writes outside this task directory
```

- [ ] **Step 2: Re-launch the container**

```bash
bash scripts/launch.sh --worksite /c/Users/OleJohanBondahl/Documents/Github_zen/fastify-test
```

Watch should pick up the smoke-test task and run a session.

- [ ] **Step 3: Verify the session completes**

Check that:
- A STATE.md was created in the task directory with status progression
- SUMMARY.md was created with a project description
- Session log exists in `simpleharness/logs/`
- The approver was invoked for any non-allowlisted commands (or nothing was blocked because all commands were in the allowlist)

- [ ] **Step 4: Clean up the smoke test**

After verification, optionally remove the smoke-test task:

```bash
rm -rf C:/Users/OleJohanBondahl/Documents/Github_zen/fastify-test/simpleharness/tasks/001-smoke-test
```

Or keep it as a record — your call.
