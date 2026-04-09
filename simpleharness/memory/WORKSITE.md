# Worksite memory

Long-term notes that every session can read.

## Task 001 — dev-container spec refinement (done)

- `docs/dev-container.md` updated: all `harness.py` refs → correct modules (core.py, shell.py, session.py), `dangerous_auto_approve` → `permissions.mode: dangerous`, §12 marked as proposed design.
- The doc is implementation-ready for downstream tasks 002/003.
- Note: `docs/usage.md` also has stale `dangerous_auto_approve` references (out of scope for task 001).

## Task 002 — create container artifacts (active, brainstorm phase)

- Kickoff complete. Refinement from task 001 applied to TASK.md.
- Next: brainstormer validates the spec (`docs/dev-container.md` §4.1–4.5) against the current codebase.
- The spec contains complete file contents for all 5 deliverables — brainstormer should focus on validation, not design.
- Key risks: Windows path handling, shellcheck compliance, `.gitattributes` coverage.
