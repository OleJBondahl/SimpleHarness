# 02 — Plan Writer (plan-writer)

**Task:** CLI error classifier and retry/backoff for unattended container operation.

## What I did

Produced `02-plan.md` — a 7-task TDD implementation plan grounded in the actual codebase.

## Process

1. Dispatched two Haiku subagents in parallel:
   - One read TASK.md, 00-kickoff.md, 01-brainstorm.md, STATE.md
   - One explored the full worksite: core.py, shell.py, session.py, io.py, test_core.py, pyproject.toml, docs/dev-container.md §12
2. Dispatched a third Haiku subagent for exact code snippets: dataclass definitions, function implementations, test factories, pattern table
3. Drafted 02-plan.md with 7 tasks following TDD (test-first) approach
4. Dispatched a Sonnet subagent to review the plan against TASK.md success criteria
5. Incorporated Sonnet's feedback:
   - Moved shell.py `plan_tick` call update from Task 6 to Task 5 (ORDER fix)
   - Added explicit `json` import guidance for shell.py
   - Extended risk #1 to cover regex constants and `@deal.pure` interaction

## Key decisions

- **Approach A (maximal purity)** from brainstorm — all classification, backoff, and state-transition logic in core.py as `@deal.pure` functions
- Added `now: datetime` parameter to `pick_next_task` and `plan_tick` (not just filtering in shell) to keep backoff-awareness testable as pure functions
- New `TickPlan` kind `"all_backoff"` for when all active tasks are in backoff
- `classify_result` is a keyword-only parameter on `compute_post_session_state` for backward compatibility
- Error text extracted from `.jsonl` log (I/O in shell), not from `SessionResult.result_text`

## Files produced

- `02-plan.md` — full implementation plan with 7 tasks, TDD steps, exact code
- `02-plan_writer.md` — this phase file
- `STATE.md` — updated: phase=plan, next_role=developer
