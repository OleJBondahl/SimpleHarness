---
title: Implement CLI error classifier and retry backoff for unattended operation
workflow: feature-build
worksite: .
depends_on:
  - 001-design-dev-container-for-safe-bypass-permissions-execution
deliverables:
  - path: src/simpleharness/core.py
    description: "Pure error classification logic and backoff computation"
  - path: src/simpleharness/shell.py
    description: "Watch-loop integration for retry/backoff and STATE.md field management"
refine_on_deps_complete: true
references:
  - docs/dev-container.md
  - docs/intent.md
  - src/simpleharness/core.py
  - src/simpleharness/shell.py
  - tests/test_core.py
---

# Goal

Make the harness resilient to transient Claude CLI failures during unattended container operation. When `claude -p` fails, the harness should classify the error (usage limit, transient, or fatal), apply appropriate backoff, and either park the task temporarily or block it for user intervention.

The end state: the harness can run overnight in a container without crashing on transient API errors, and surfaces permanent failures clearly in task state and logs.

## Success criteria

- [ ] A pure classifier function in `core.py` maps CLI exit codes and error messages to one of three outcomes: `usage_limit`, `transient`, `fatal`
- [ ] Unknown errors default to `fatal` (loud stop, not silent retry)
- [ ] `STATE.md` supports optional `retry_count` and `retry_after` fields
- [ ] The watch loop skips tasks whose `retry_after` is in the future
- [ ] Transient failures use a fixed backoff schedule, escalating to `fatal` after a configured number of retries
- [ ] Usage-limit failures park the task until the reported reset time without bumping retry count
- [ ] Fatal failures set `status: blocked` with a clear reason
- [ ] Successful sessions clear retry state
- [ ] All new core logic is decorated with `@deal.pure` and tested
- [ ] `uv run pytest` passes with no regressions
- [ ] `uv run ruff check .` exits 0
- [ ] `uv run ty check` exits 0

## Boundaries

- Stay on the `feature/dev-container` branch — do not create new branches
- Respect the FC/IS split: classification logic and backoff computation go in `core.py` (pure), STATE.md I/O and watch-loop changes go in `shell.py` (impure)
- Do not modify container artifacts (Dockerfile, compose.yml, scripts/) — that is task 002
- Do not break the existing approver hook flow
- Do not add automatic re-login or token refresh — that is out of scope for v0.1

## Autonomy

**Pre-authorized (decide and proceed):**
- Internal naming of the classifier function and data types
- The exact regex patterns for error signal matching
- The specific backoff schedule values (the spec suggests `[30, 60, 120, 240, 300]` but adjust if you have reason to)
- Test structure and fixtures
- How `retry_after` is serialized in STATE.md (ISO timestamp is preferred)

**Must block (stop and write BLOCKED.md):**
- The watch loop architecture doesn't support per-task skip logic without a significant refactor
- The STATE.md parser would need breaking changes to support the new fields
- The classifier needs access to data the harness doesn't currently capture (e.g., stream-json events aren't being logged)
- Changes would break the existing approver hook integration

## Handoff

Task 004 (documentation) may reference the retry behavior in usage docs. The error classification outcomes and user-visible log messages should be clear enough for docs to describe without reading the source.

## Notes

The design in `docs/dev-container.md` section 12 describes the proposed approach. The FC/IS architecture is already established — new pure functions in `core.py` must be `@deal.pure` decorated. The existing test suite has ~125 tests with ~99% core coverage; maintain that standard.
