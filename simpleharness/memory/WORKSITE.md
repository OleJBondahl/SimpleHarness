# Worksite memory

Long-term notes that every session can read.

## Task 001 — dev-container spec refinement (done)

- `docs/dev-container.md` updated: all `harness.py` refs → correct modules (core.py, shell.py, session.py), `dangerous_auto_approve` → `permissions.mode: dangerous`, §12 marked as proposed design.
- The doc is implementation-ready for downstream tasks 002/003.
- Note: `docs/usage.md` also has stale `dangerous_auto_approve` references (out of scope for task 001).

## Task 002 — create container artifacts (active, plan → developer)

- Brainstorm complete. All 6 spec line references validated — all match (minor drift: `session.py` ends at line 75 not 73, no impact).
- Plan complete (`02-plan.md`). 9 tasks, 5 deliverables: Dockerfile, compose.yml, launch.sh, entrypoint.sh, .dockerignore.
- **Approach:** Transcribe §4.1–4.5 verbatim, validate with shellcheck + `docker compose config` + `docker compose build`. No deviations from spec.
- **Developer notes:** Pre-flight checks (Docker Desktop, shellcheck) are Step 1. Commit after each file. Integration validation (build + sandbox env check) is Task 8. All code lives in `docs/dev-container.md` §4.1–4.5.
- Key risks: shellcheck compliance, named-volume shadowing, Windows path handling (host-only test).
