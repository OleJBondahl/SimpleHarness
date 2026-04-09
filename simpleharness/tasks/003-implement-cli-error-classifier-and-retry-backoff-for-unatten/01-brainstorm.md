# 01 — Brainstorm (brainstormer)

**Task:** CLI error classifier and retry/backoff for unattended container operation.

## Context

Read and verified the following against the current codebase:

- **TASK.md**: well-scoped brief with clear success criteria, boundaries, and autonomy grants
- **00-kickoff.md**: project-leader routing to brainstormer with four specific verification items
- **docs/dev-container.md §12**: detailed proposed design — pattern table, backoff schedule, STATE.md fields, watch-loop integration points
- **src/simpleharness/core.py**: frozen dataclasses (`State`, `SessionResult`, `Task`), pure functions (`compute_post_session_state`, `plan_tick`, `pick_next_task`), all `@deal.pure`
- **src/simpleharness/shell.py**: `tick_once()` orchestrates session lifecycle — calls `plan_tick`, `run_session`, `compute_post_session_state`, `write_state`
- **src/simpleharness/session.py**: `run_session()` returns `SessionResult(exit_code, result_text, ...)`, streams to `.jsonl` log
- **src/simpleharness/io.py**: `read_state`/`write_state` manually map fields — must be extended for new fields

## Clarifying questions

1. **Where does the classifier get its error text?** `[self-answerable]`
   `SessionResult` has `exit_code` and `result_text`. The `.jsonl` log has stream-json error events. The simplest path: after `run_session` returns, read the `.jsonl` log for error events (I/O in shell), extract error messages, pass them to the pure classifier alongside `exit_code`. Alternatively, capture the last error event during `stream_and_log` and attach it to `SessionResult`. Either works; the second keeps the classifier call cleaner.

2. **Do `read_state`/`write_state` break with new fields?** `[self-answerable]`
   No. Both use explicit field mapping with `meta.get(key, default)`. Adding `retry_count: int = 0` and `retry_after: str | None = None` to `State` with defaults, then adding corresponding lines to `read_state`/`write_state` and `_STATE_FIELD_ORDER`, is additive and non-breaking. Old STATE.md files without these fields parse fine.

3. **Does `plan_tick`/`pick_next_task` support per-task skip?** `[self-answerable]`
   `pick_next_task` filters by `status == "active"`. Tasks in backoff stay active but should be skipped. Two options: (a) filter tasks with future `retry_after` before passing to `pick_next_task`, or (b) add the filter inside `plan_tick`. Option (a) is simpler — add it where `plan_tick` builds its candidate list. ~5 lines. No significant refactor needed.

4. **Does the correction override `retry_after`?** `[self-answerable]`
   Yes per spec. The correction path in `pick_next_task` gives priority to tasks with `CORRECTION.md`. The `retry_after` filter must exempt tasks with corrections: if a task has a correction, skip the backoff check. This is a ~2-line guard.

5. **Does `compute_post_session_state` handle retry transitions?** `[self-answerable]`
   Currently it handles success + loop guards (session cap, same-role repeat). The retry/backoff logic is a new outcome path. The cleanest approach: extend `compute_post_session_state` to accept the classifier result and produce the appropriate state (including retry fields). This keeps the state-transition logic pure and testable.

6. **Does the harness capture enough data for the pattern table?** `[self-answerable]`
   Exit code: yes, from `proc.returncode`. Error text: the `.jsonl` log captures `{"type":"error",...}` events, and `result_text` captures the last assistant message. The spec's pattern table matches against "stream-json error body or last stderr line" — both are available. No new data capture needed, just extraction.

**No `[needs user]` questions. No blocking conditions from the autonomy section.**

Verified all four "must block" conditions:
- ✓ Watch loop supports per-task skip — `plan_tick` filter addition, not a refactor
- ✓ STATE.md parser is additive — new optional fields with defaults
- ✓ Classifier data is already captured — exit code + .jsonl error events
- ✓ Approver hook is unaffected — retry logic is post-session, different code path

## Possible approaches

### A. Extend `compute_post_session_state` (maximal purity)

Add the classifier and backoff computation as pure functions in `core.py`. Extend `compute_post_session_state` to accept a `ClassifyResult` and produce the full post-session state including retry fields. Shell changes are minimal: extract error text from the `.jsonl` log after session, call pure classifier, pass result to the existing pure state-transition function.

**Trade-off:** Most logic is pure and testable (~99% core coverage maintained). `compute_post_session_state` signature grows slightly. Requires extracting error text in the shell layer.

### B. Separate retry handler in `tick_once` (pragmatic split)

Add classifier + backoff functions to `core.py` (pure), but handle the state-branching (success vs usage_limit vs transient vs fatal) as shell orchestration in `tick_once`. The pure functions answer "what kind of failure?" and "when to retry?", but `tick_once` wires the results into `State` and calls `write_state`.

**Trade-off:** Less churn to `compute_post_session_state`. But puts state-transition logic in shell.py, diluting the FC/IS split. More integration test surface needed.

### C. New dedicated retry module (over-engineered)

Create `retry_core.py` + `retry_shell.py` to fully encapsulate retry concerns. Complete isolation, own test file.

**Trade-off:** Clean separation, but massive over-abstraction for ~50 lines of pure logic and ~30 lines of shell integration. Adds cognitive overhead (more files to navigate) for zero practical benefit.

## Recommended direction

**Approach A — maximal purity.** The classifier function (`(exit_code, error_text) → ClassifyResult`) is a textbook pure function: deterministic, no I/O, pattern-match on inputs, return a tagged result. The backoff schedule lookup (`(retry_count, schedule) → seconds`) is trivially pure. The post-session state transition (`(current_state, classifier_result, now) → new_state`) is the same kind of logic `compute_post_session_state` already does. Putting all three in `core.py` with `@deal.pure` means they're covered by the existing FP gate, deal-lint, and the ~99% test coverage standard. The shell layer's only new job is extracting error text from the `.jsonl` log — a thin I/O operation that belongs in shell.py. This approach aligns with the established architecture and keeps the test surface focused on pure functions.
