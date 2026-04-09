# Worksite memory

Long-term notes that every session can read.

## Task 001 — dev-container spec refinement (done)

- `docs/dev-container.md` updated: all `harness.py` refs → correct modules (core.py, shell.py, session.py), `dangerous_auto_approve` → `permissions.mode: dangerous`, §12 marked as proposed design.
- The doc is implementation-ready for downstream tasks 002/003.
- Note: `docs/usage.md` also has stale `dangerous_auto_approve` references (out of scope for task 001).

## Task 002 — create container artifacts (active, brainstorm → plan-writer)

- Brainstorm complete. All 6 spec line references validated against current codebase — all match (one minor drift: `session.py` function ends at line 75 not 73, no impact).
- `.gitattributes` wildcard `* text=auto eol=lf` already covers all new files — no additions needed.
- `simpleharness init`, `--worksite` flag, and `uv tool install -e` all confirmed to exist.
- **Recommended approach:** Transcribe §4.1–4.5 code blocks verbatim, run shellcheck + `docker compose config` as validation. No deviations from spec identified.
- Key risks remain: shellcheck compliance, named-volume shadowing, Windows path handling (can only fully test on host).
