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
