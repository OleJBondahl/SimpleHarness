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

## Task 003 — CLI error classifier and retry backoff (done)

- **Complete.** 8 commits on `feature/dev-container` (`442dc30`..`5c9dd62`), 329 tests passing, all quality gates clean.
- **What was built:** Pure classifier (`classify_cli_error`) maps CLI errors to `usage_limit | transient | fatal`. Fixed backoff schedule `(30, 60, 120, 240, 300)`s with escalation to fatal. Watch loop skips tasks in backoff. Usage-limit parking without retry bump. Fatal → `status: blocked`.
- **Security fixes applied:** timezone-naive comparison, malformed retry_after handling, over-broad timeout pattern.
- **Known low-priority items:** ReDoS in usage-limit regex, unbounded `.jsonl` read, no `blocked_reason` length cap.
- Task 004 (documentation) can reference the three error outcomes and backoff behavior.
