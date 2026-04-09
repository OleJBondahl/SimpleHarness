# 03 — Develop (developer)

**Task:** CLI error classifier and retry/backoff for unattended container operation.

## Summary

Executed the 7-task TDD plan via subagent-driven development. All pure logic in `core.py`, shell integration in `shell.py`. Expert-critic security review found 2 critical issues (timezone-naive comparison, unhandled ValueError on malformed retry_after) — both fixed in a follow-up commit.

## Step Log

### Task 1: Add retry fields to State dataclass and update io.py
- **Subagent:** Sonnet implementer
- **Files changed:** `core.py` (+3), `io.py` (+6), `tests/test_core.py` (+43)
- **Tests added:** 4 (retry field defaults, set, roundtrip, roundtrip defaults)
- **Result:** PASS — 121 tests
- **Commit:** `442dc30`

### Task 2: Add ClassifyResult and classify_cli_error function
- **Subagent:** Sonnet implementer
- **Files changed:** `core.py` (ErrorOutcome type, ClassifyResult dataclass, regex constants, classify_cli_error function), `tests/test_core.py` (+16 tests)
- **Tests added:** 16 (usage_limit, transient patterns x8, fatal patterns x4, unknown, empty, priority)
- **Result:** PASS — 137 tests
- **Commit:** `1e0979c`

### Task 3: Add compute_backoff_delay function
- **Subagent:** Sonnet implementer
- **Files changed:** `core.py` (DEFAULT_BACKOFF_SCHEDULE, compute_backoff_delay), `tests/test_core.py` (+9 tests)
- **Tests added:** 9 (each retry level, exhausted, custom schedule, schedule values)
- **Result:** PASS — 146 tests
- **Commit:** `ea3a464`

### Task 4: Extend compute_post_session_state for retry logic
- **Subagent:** Sonnet implementer
- **Files changed:** `core.py` (timedelta import, classify_result kwarg, retry logic block), `tests/test_core.py` (+7 tests)
- **Tests added:** 7 (clear on success, transient bump, correct backoff, exhausted blocks, usage_limit parks, fatal blocks, no classify_result backward compat)
- **Note:** Plan's exhaustion test had off-by-one (retry_count=4 doesn't exhaust 5-entry schedule). Fixed to retry_count=5.
- **Result:** PASS — 153 tests
- **Commit:** `e0f95c3`

### Task 5: Backoff-aware task selection in pick_next_task and plan_tick
- **Subagent:** Sonnet implementer
- **Files changed:** `core.py` (TickPlan kind, pick_next_task now param + backoff filter, plan_tick now param + all_backoff), `shell.py` (plan_tick now arg, all_backoff case), `tests/test_core.py` (+7 tests, 23 existing calls updated)
- **Tests added:** 7 (skip backoff, past backoff, correction overrides, no retry_after, all_backoff plan, run with now)
- **Result:** PASS — 159 tests
- **Commit:** `f21698a`

### Task 6: Shell integration — extract errors and wire classifier
- **Subagent:** Sonnet implementer
- **Files changed:** `shell.py` (json import, classify_cli_error import, _extract_error_text helper, classifier wiring in tick_once, classify_result passed to compute_post_session_state)
- **Result:** PASS — 326 tests (full suite)
- **Commit:** `5447961`

### Task 7: Full verification
- **Ran by:** Haiku verifier + direct Bash
- 326 tests pass, ruff clean, ty clean, FP purity gate clean

## Critique

**Expert-critic agent** (security & resilience focus) reviewed all changes:

### Fixed (CRITICAL)
1. **Timezone-naive comparison** — `_USAGE_LIMIT_RE` regex could capture timestamps without `Z`, creating naive datetimes that crash when compared with aware `now`. Fix: normalize by appending `Z` in `classify_cli_error`.
2. **Unhandled ValueError** — malformed `retry_after` in STATE.md (hand-edit or corruption) crashes `fromisoformat`. Fix: added `_parse_retry_after` helper with try/except; invalid timestamps treated as "not in backoff" (safe default).
3. **Over-broad "timeout" pattern** — `r"timeout"` matched non-network timeouts. Fix: tightened to `r"(?:connection|request|read|connect)\s*time[d ]?\s*out"`.

- **Commit:** `e382096`
- **Tests added:** 3 (bare timestamp Z normalization, invalid retry_after not skipped, generic timeout is fatal)
- **Final count:** 329 tests passing

### Noted (not fixed — low priority)
- ReDoS potential in `_USAGE_LIMIT_RE` (greedy `.*` quantifiers) — unlikely with real CLI error messages
- Unbounded memory in `_extract_error_text` (reads full .jsonl) — acceptable for current session sizes
- No length cap on `blocked_reason` — would be cleaner with truncation but not dangerous

## Verification

```
uv run pytest -q           → 329 passed in 0.57s
uv run ruff check .        → All checks passed!
uv run ty check            → All checks passed!
fp-purity-gate core.py     → All functions decorated
```
