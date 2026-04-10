# Fastify-Test Dev Container Setup

Wire SimpleHarness's existing container infrastructure to run against `fastify-test` with approver-mode permissions.

## Worksite

- **Path**: `C:\Users\OleJohanBondahl\Documents\Github_zen\fastify-test`
- **Mounted at**: `/worksite` inside the container (via compose.yml)

## Permission Mode

Approver mode: Sonnet reviews any command that falls outside the static allowlist.

### Per-worksite config (`fastify-test/simpleharness/config.yaml`)

```yaml
permissions:
  mode: approver
  approver_model: sonnet
  extra_bash_allow:
    - "npm *"
    - "npx *"
    - "vitest *"
    - "tsc *"
```

This extends the default allowlist (git, uv, npm, pytest, ruff, node, ls, cat, etc.) with Node/TypeScript-specific tooling.

## Launch

From Git Bash on the host:

```bash
bash scripts/launch.sh --worksite /c/Users/OleJohanBondahl/Documents/Github_zen/fastify-test
```

`launch.sh` handles:
- cygpath normalization for Windows paths
- Docker image build
- Credential volume probe (prints login command if needed)
- `docker compose run --rm simpleharness`

`entrypoint.sh` handles first-run bootstrap:
- git config (autocrlf, safe.directory)
- `uv tool install -e /opt/simpleharness`
- `simpleharness init` (scaffolds tasks/, memory/, logs/)
- Writes per-worksite config if not present

## Task Workflow

1. Create a task directory: `fastify-test/simpleharness/tasks/<NNN-slug>/`
2. Write a `TASK.md` with frontmatter (title, workflow) and goal/criteria sections
3. Launch the container — `simpleharness watch` picks up active tasks automatically

## Scope

### In scope
- Create `fastify-test/simpleharness/config.yaml` with approver mode + Node allowlist
- Create `fastify-test/simpleharness/tasks/` directory structure via init
- Verify launch works end-to-end

### Out of scope
- No changes to SimpleHarness source code, Dockerfile, or compose.yml
- No new scripts
- No specific task authoring (user creates tasks ad-hoc)
