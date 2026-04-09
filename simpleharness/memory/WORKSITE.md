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

## Task 003 — CLI error classifier and retry backoff (developer complete)

- **Implementation complete.** 7 commits on `feature/dev-container`, 329 tests passing.
- **What was built:**
  - `State` dataclass: `retry_count` (int) and `retry_after` (ISO str | None) fields, roundtrip through `io.py`
  - `ClassifyResult` dataclass + `classify_cli_error` pure function: regex pattern table maps error text to `usage_limit | transient | fatal`
  - `compute_backoff_delay`: fixed schedule `(30, 60, 120, 240, 300)` seconds
  - `compute_post_session_state` extended: clears retry on success, bumps on transient, parks on usage_limit, blocks on fatal
  - `pick_next_task` and `plan_tick` gain `now: datetime` param for backoff-aware filtering; new `all_backoff` TickPlan kind
  - `shell.py`: `_extract_error_text` reads `.jsonl` logs, classifier wired into `tick_once`
- **Security review fixes:** timezone-naive comparison (appends Z), malformed retry_after handling (`_parse_retry_after`), tightened timeout pattern
- **Known low-priority items:** ReDoS potential in usage-limit regex (bounded by real CLI message lengths), unbounded `.jsonl` read (acceptable for current sizes), no length cap on `blocked_reason`
- Next: project-leader reviews and wraps up.
- Branch: `feature/dev-container`.
