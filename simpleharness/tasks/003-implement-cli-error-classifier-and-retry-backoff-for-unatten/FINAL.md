# FINAL — Task 003: CLI Error Classifier and Retry/Backoff

**Verdict: SUCCESS**

## Summary

The harness is now resilient to transient Claude CLI failures during unattended container operation. A pure error classifier in `core.py` maps CLI exit codes and error messages to three outcomes (`usage_limit`, `transient`, `fatal`), with fixed backoff escalation for transient errors and clean parking for usage-limit windows. The watch loop skips tasks in backoff and surfaces permanent failures via `status: blocked`.

All new logic follows the FC/IS split: classification, backoff computation, and state transitions are `@deal.pure` functions in `core.py`; the shell layer extracts error text from `.jsonl` logs and wires the classifier into the existing `tick_once` flow.

## Success Criteria — All Met

- Pure classifier function maps to `usage_limit | transient | fatal`
- Unknown errors default to `fatal`
- `STATE.md` supports `retry_count` and `retry_after` fields
- Watch loop skips tasks whose `retry_after` is in the future
- Transient failures use `[30, 60, 120, 240, 300]`s backoff, escalate to fatal after 5 retries
- Usage-limit failures park until reset time without bumping retry count
- Fatal failures set `status: blocked` with clear reason
- Successful sessions clear retry state
- All core logic `@deal.pure` decorated and tested
- 329 tests passing, ruff clean, ty clean

## Artifacts Changed

| File | Change |
|------|--------|
| `src/simpleharness/core.py` | +`ClassifyResult`, `ErrorOutcome`, `classify_cli_error`, `compute_backoff_delay`, `DEFAULT_BACKOFF_SCHEDULE`, `_parse_retry_after`; extended `State` (retry fields), `compute_post_session_state`, `pick_next_task`, `plan_tick` |
| `src/simpleharness/io.py` | Extended `read_state`/`write_state` for `retry_count` and `retry_after` |
| `src/simpleharness/shell.py` | Added `_extract_error_text`, wired classifier into `tick_once`, backoff-aware `plan_tick` call |
| `tests/test_core.py` | +53 tests (classifier, backoff, retry state, task selection, security fixes) |

## Commits

| Hash | Message |
|------|---------|
| `442dc30` | feat(core): add retry_count and retry_after fields to State dataclass |
| `1e0979c` | feat(core): add classify_cli_error pure function with pattern table |
| `ea3a464` | feat(core): add compute_backoff_delay pure function |
| `e0f95c3` | feat(core): extend compute_post_session_state with retry/backoff logic |
| `f21698a` | feat(core): add backoff-aware task selection to pick_next_task and plan_tick |
| `5447961` | feat(shell): wire CLI error classifier into tick_once with retry/backoff |
| `e382096` | fix(core): address critical timezone and error handling issues from security review |
| `5c9dd62` | docs: complete developer phase for task 003 |

## Notes for Downstream

- Task 004 (documentation) can reference the three error outcomes and backoff behavior.
- The `_USAGE_LIMIT_RE` has a noted low-priority ReDoS concern with greedy `.*` — acceptable for real CLI output but worth noting if the pattern table grows.
- `_extract_error_text` reads the full `.jsonl` log — fine for current session sizes but could need streaming for very long sessions.
