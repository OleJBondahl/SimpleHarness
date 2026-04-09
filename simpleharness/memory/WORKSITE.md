# Worksite memory

Long-term notes that every session can read.

## Task 001 — dev-container spec refinement (done)

- `docs/dev-container.md` updated: all `harness.py` refs → correct modules (core.py, shell.py, session.py), `dangerous_auto_approve` → `permissions.mode: dangerous`, §12 marked as proposed design.
- The doc is implementation-ready for downstream tasks 002/003.
- Note: `docs/usage.md` also has stale `dangerous_auto_approve` references (out of scope for task 001).

## Task 002 — create container artifacts (done)

- All 5 artifacts created, reviewed, and committed on `feature/dev-container`:
  - `.dockerignore` (`e5edf65`), `Dockerfile` (`99c74c9`), `compose.yml` (`576c6c5`), `scripts/entrypoint.sh` (`6b5a1c9`), `scripts/launch.sh` (`0341b41`)
- Security fix: `safe.directory` wildcard → explicit paths; `.env` added to `.dockerignore` (`b1ce978`).
- **Pre-merge gates (not yet run):** `shellcheck`, `docker compose config`, `docker compose build` — blocked by session permissions in all phases. Must run manually before merging.
- Branch is 13 commits ahead of `origin/feature/dev-container`.

## Task 003 — CLI error classifier and retry backoff (in progress)

- **Brainstorm complete.** All four "must block" conditions verified clear:
  - Watch loop supports per-task skip via `plan_tick` filter (~5 lines, not a refactor)
  - STATE.md parser is additive — new optional fields `retry_count`/`retry_after` with defaults
  - Classifier data already captured — `exit_code` from `proc.returncode`, error text from `.jsonl` stream log
  - Approver hook unaffected — retry logic is post-session path
- **Recommended approach:** maximal purity (Approach A). Classifier, backoff computation, and state transitions all go in `core.py` with `@deal.pure`. Shell layer only extracts error text from `.jsonl` log.
- **Key integration points:**
  - `core.py`: new `ClassifyResult` type, `classify_cli_error()`, backoff schedule, extend `compute_post_session_state`
  - `core.py State`: add `retry_count: int = 0`, `retry_after: str | None = None`
  - `io.py`: extend `read_state`/`write_state` + `_STATE_FIELD_ORDER`
  - `shell.py tick_once`: extract error text post-session, call classifier, pass to compute function; add `retry_after` filter in `plan_tick`
- **Plan complete** (`02-plan.md`): 7-task TDD plan, Sonnet-reviewed. Key design choices:
  - `now: datetime` added to `pick_next_task` and `plan_tick` signatures (pure backoff filtering)
  - New `TickPlan` kind `"all_backoff"` for when all candidates are in backoff
  - `classify_result` is keyword-only on `compute_post_session_state` (backward-compatible)
  - Error text extracted from `.jsonl` log in shell, not from `SessionResult`
- **Risk to watch:** `@deal.pure` may flag `datetime.fromisoformat` or module-level `re.compile` constants — see risk #1 in plan
- **Existing test call-sites:** all `plan_tick()` and `pick_next_task()` calls in tests must gain a `now` arg (Task 5 Step 6)
- Next: developer implements the plan.
- Branch: `feature/dev-container` (same as tasks 001/002).
