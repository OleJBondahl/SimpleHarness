# 00 — Kickoff (project-leader)

**Task:** Implement CLI error classifier and retry/backoff for unattended container operation.

## Summary

The harness needs resilience against transient Claude CLI failures during overnight container runs. The design is already specified in `docs/dev-container.md` section 12 and covers:

- **Two new optional STATE.md fields**: `retry_count` (int) and `retry_after` (ISO timestamp)
- **One pure classifier function** in `core.py`: maps exit codes + error messages to `usage_limit | transient | fatal`
- **Fixed backoff schedule**: `[30, 60, 120, 240, 300]` seconds, escalating to fatal after 5 transient retries
- **Watch-loop integration** in `shell.py`: skip tasks in backoff, handle each outcome after session exit, clear retry state on success

Unknown errors default to `fatal` (loud stop, not silent retry).

## Upstream dependency

Task 001 (design) is complete. `NEEDS_REFINEMENT.md` confirms `docs/dev-container.md` is implementation-ready. Section 12 is the authoritative spec for this task.

## Decision: first role

Sending to **brainstormer** next. Even though the design is detailed, the brainstormer should:

1. Read the current `core.py`, `shell.py`, and `session.py` to verify the spec's assumptions about spawn_claude(), tick_once(), and STATE.md parsing still hold
2. Identify any gaps between the spec and codebase reality (e.g., does the YAML frontmatter parser handle new optional fields without changes?)
3. Flag any "must block" conditions from the TASK.md autonomy section
4. Confirm the pattern table and detection approach are feasible with the data currently available from CLI sessions

## Boundaries reminder for downstream roles

- Stay on `feature/dev-container` branch
- FC/IS split: pure logic in `core.py`, I/O in `shell.py`
- Do not modify container artifacts (Dockerfile, compose.yml, scripts/)
- Do not break the approver hook flow
- All new core functions must be `@deal.pure` decorated and tested
