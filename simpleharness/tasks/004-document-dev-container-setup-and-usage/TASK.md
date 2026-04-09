---
title: Document dev container setup and usage
workflow: universal
worksite: .
depends_on:
  - 002-create-container-artifacts-for-dev-container
  - 003-implement-cli-error-classifier-and-retry-backoff-for-unatten
deliverables:
  - path: docs/dev-container-usage.md
    description: "User-facing guide for setting up and using the dev container with any Python or JS/TS repo"
refine_on_deps_complete: true
references:
  - docs/dev-container.md
  - docs/usage.md
  - docs/intent.md
  - Dockerfile
  - compose.yml
  - scripts/launch.sh
  - scripts/entrypoint.sh
---

# Goal

Write clear, user-facing documentation that explains how to set up and use the SimpleHarness dev container with any Python or JavaScript/TypeScript repository. The audience is a solo developer on Windows who wants to run SimpleHarness autonomously against their own repos.

The end state: a new developer can go from zero to a running container by following the doc, without needing to read the design spec or source code.

## Success criteria

- [ ] `docs/dev-container-usage.md` exists with at minimum: prerequisites, first-run setup, steady-state usage, troubleshooting, and cleanup sections
- [ ] Prerequisites section lists exact tools and versions needed (Docker Desktop, Git Bash, etc.)
- [ ] First-run walkthrough covers clone, launch, login, and re-launch — with copy-pasteable commands
- [ ] Covers usage with both Python repos and JS/TS repos (any differences in setup)
- [ ] Documents the `--allow-toolbox-edits` flag and when to use it
- [ ] Documents error handling behavior (what the user sees when the harness retries vs blocks)
- [ ] Includes a troubleshooting section for common issues (CRLF, path issues, credential expiry, stale image)
- [ ] No changes to any source code or container artifacts — documentation only
- [ ] Links to or references the design spec for readers who want deeper technical context

## Boundaries

- Stay on the `feature/dev-container` branch — do not create new branches
- Do not modify any source code, container artifacts, or configuration files
- Do not duplicate the design spec — link to `docs/dev-container.md` for architecture details
- Keep it practical and concise — this is a usage guide, not a design document

## Autonomy

**Pre-authorized (decide and proceed):**
- Document structure, section ordering, and formatting choices
- Which troubleshooting scenarios to include based on the known limitations in the spec
- Whether to add the doc to an existing docs index or table of contents

**Must block (stop and write BLOCKED.md):**
- The container artifacts from task 002 don't work as documented and need changes
- The error handling from task 003 behaves differently than expected and the docs can't accurately describe it

## Notes

The existing `docs/usage.md` covers the harness CLI usage. This new doc should complement it, not overlap. Cross-reference where appropriate. The design spec (`docs/dev-container.md`) has detailed technical context but is not user-friendly — distill it into practical guidance.

## Refinement from 003-implement-cli-error-classifier-and-retry-backoff-for-unatten

Task 003 is complete. Here are the concrete error-handling behaviors to document:

**Three error outcomes** (from `classify_cli_error` in `core.py`):
- **`usage_limit`** — Claude API usage/rate limit hit. The harness parks the task until the reported reset time (stored in `retry_after` as an ISO 8601 timestamp). Does NOT count as a retry. The task resumes automatically after the window passes.
- **`transient`** — Network timeouts, 503s, "overloaded" errors. The harness retries with a fixed backoff schedule: 30s, 60s, 120s, 240s, 300s. After 5 consecutive transient failures, the task is marked `status: blocked` with a clear reason.
- **`fatal`** — Auth failures, invalid model, unknown errors. The task is immediately marked `status: blocked` with a `blocked_reason` explaining the failure. Unknown errors default to fatal (loud stop, not silent retry).

**What the user sees in STATE.md:**
- `retry_count: N` — how many consecutive transient retries have occurred (0 on success or usage_limit)
- `retry_after: 2026-04-09T16:30:00Z` — ISO timestamp; the harness skips this task until this time passes
- `status: blocked` + `blocked_reason: "..."` — permanent failure requiring user intervention

**Behavior on success:** Both `retry_count` and `retry_after` are cleared, so the task returns to normal scheduling.

**User-visible log messages:** Error text is extracted from the session's `.jsonl` log file. The `blocked_reason` in STATE.md contains the classifier's reason string, which is human-readable.
