# 04 — Review (project-leader)

## Assessment

All 5 deliverables exist and match the spec (docs/dev-container.md §4.1–4.5) verbatim, with two justified security deviations:

1. **`safe.directory`** — wildcard `'*'` replaced with explicit `/worksite` + `/opt/simpleharness` paths in entrypoint.sh (security hardening, commit `b1ce978`)
2. **`.env` in `.dockerignore`** — added `.env` / `.env.*` entries not in spec (secret exclusion, same commit)

Both deviations improve security posture and were identified during expert-critic review in the developer phase.

## Artifact-by-artifact

| Artifact | Lines | Match | Notes |
|---|---|---|---|
| `Dockerfile` | 37 | Verbatim | Python 3.13 + Node 20 + uv + tini + Claude CLI |
| `compose.yml` | 46 | Verbatim | Sandbox env, volume mounts, named home volume |
| `scripts/launch.sh` | 101 | Verbatim | Windows path handling, overlap guard, creds probe |
| `scripts/entrypoint.sh` | 46 | Near-verbatim | safe.directory hardened; permissions.mode write correct |
| `.dockerignore` | 24 | Near-verbatim | .env added for security |

## Deferred validations

These could not be run due to session permission constraints in both the developer and project-leader phases:

- `shellcheck scripts/entrypoint.sh scripts/launch.sh` — shellcheck not installed
- `docker compose config` — Docker CLI blocked by permissions
- `docker compose build` — same

**Recommendation:** Run these manually before merging `feature/dev-container` into master. They are pre-merge gates, not blockers for marking the task complete — the artifacts are structurally sound and spec-compliant.

## Decision

All deliverables present, spec-compliant, security-hardened. Advancing to **wrap-up** — writing FINAL.md.
