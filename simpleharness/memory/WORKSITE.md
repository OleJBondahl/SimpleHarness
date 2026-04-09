# Worksite memory

Long-term notes that every session can read.

## Task 001 — dev-container spec refinement (done)

- `docs/dev-container.md` updated: all `harness.py` refs → correct modules (core.py, shell.py, session.py), `dangerous_auto_approve` → `permissions.mode: dangerous`, §12 marked as proposed design.
- The doc is implementation-ready for downstream tasks 002/003.
- Note: `docs/usage.md` also has stale `dangerous_auto_approve` references (out of scope for task 001).

## Task 002 — create container artifacts (develop → project-leader)

- All 5 artifacts created and committed individually on `feature/dev-container`:
  - `.dockerignore` (`e5edf65`), `Dockerfile` (`99c74c9`), `compose.yml` (`576c6c5`), `scripts/entrypoint.sh` (`6b5a1c9`), `scripts/launch.sh` (`0341b41`)
- **Security fix applied:** `safe.directory '*'` → explicit `/worksite` + `/opt/simpleharness` in entrypoint.sh. Also added `.env` to `.dockerignore`. Commit `b1ce978`.
- **Deferred validations:** shellcheck (not installed), `docker compose config`, `docker compose build`, sandbox env check — all blocked by session permissions. Project-leader should validate before merge.
- `.gitattributes` confirmed: `* text=auto eol=lf` covers all new files.
- One spec deviation: `safe.directory` restricted to explicit paths (was `'*'` in spec) for security.
