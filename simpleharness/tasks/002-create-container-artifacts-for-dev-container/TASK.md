---
title: Create container artifacts for dev container
workflow: feature-build
worksite: .
depends_on:
  - 001-design-dev-container-for-safe-bypass-permissions-execution
deliverables:
  - path: Dockerfile
    description: "Container image definition for SimpleHarness sandbox"
  - path: compose.yml
    description: "Docker Compose service configuration"
  - path: scripts/launch.sh
    description: "Host-side launcher script"
  - path: scripts/entrypoint.sh
    description: "Container entrypoint bootstrap script"
  - path: .dockerignore
    description: "Build context exclusions"
refine_on_deps_complete: true
references:
  - docs/dev-container.md
  - docs/intent.md
---

# Goal

Create the Docker container infrastructure that lets SimpleHarness run with bypass permissions safely inside a sandboxed environment. A user should be able to point the launcher at any Python or TypeScript git repo and have the harness running in a container within minutes (first run) or seconds (subsequent runs).

The end state: `scripts/launch.sh` works on Windows 11 + Git Bash + Docker Desktop, building the image, probing credentials, and starting the harness watch loop against a user-specified worksite.

## Success criteria

- [ ] `docker compose build` succeeds and produces a working image with Python 3.13, Node, uv, and Claude CLI
- [ ] `scripts/launch.sh` validates the worksite, refuses overlapping toolbox/worksite paths, and launches the container
- [ ] `scripts/launch.sh` detects missing Claude credentials and prints the login command
- [ ] The entrypoint installs simpleharness from the bind-mounted toolbox on first run
- [ ] The harness inside the container recognizes it is sandboxed (sandbox check passes)
- [ ] Windows path handling works (CRLF normalization, cygpath conversion, MSYS_NO_PATHCONV)
- [ ] `.gitattributes` covers all new files for LF normalization
- [ ] All new shell scripts pass `shellcheck` with no errors

## Boundaries

- Stay on the `feature/dev-container` branch — do not create new branches
- Do not modify the existing harness Python source (core.py, shell.py, approver_core.py, approver_shell.py)
- Do not modify existing tests
- Do not implement error handling / retry logic — that is task 003
- Linux host support is out of scope (Windows 11 + Docker Desktop only for v0.1)

## Autonomy

**Pre-authorized (decide and proceed):**
- Exact Dockerfile layer ordering and optimization choices
- Shell script implementation details (argument parsing, error messages, etc.)
- Choice of base image variant as long as Python 3.13 is included
- Adding entries to `.gitignore` for container-related generated files
- Minor deviations from the spec where the current codebase requires it

**Must block (stop and write BLOCKED.md):**
- The sandbox gating mechanism needs source code changes to work with the container
- The `config.yaml` structure doesn't support per-worksite dangerous-mode opt-in as spec'd
- Docker Desktop on Windows can't handle the proposed volume mount strategy
- Any new Python dependency would be needed

## Handoff

Task 004 (documentation) consumes these artifacts as the basis for user-facing setup/usage docs. The artifacts should include inline comments explaining non-obvious choices.

## Notes

The spec in `docs/dev-container.md` (refined by task 001) is the primary guide. Follow it closely but adapt where needed based on the actual codebase.
