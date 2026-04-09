# 00 — Kickoff (project-leader)

## Task

Validate and refine `docs/dev-container.md` into an implementation-ready spec by
verifying all code references against the current codebase and resolving stale
assumptions from the pre-refactor era.

## What I found

The draft was written when the codebase was a monolithic `harness.py`. Since then,
the FC/IS refactor split it into `core.py`, `shell.py`, `session.py`, and other
modules. Every code reference in the doc was stale.

### Issues identified and fixed

1. **Five stale `harness.py` references** — all replaced with correct module:line:
   - `harness.py:215` → `core.py:177` (toolbox_root)
   - `harness.py:1248-1264` → `shell.py:699-709` (sandbox check)
   - `harness.py:599-642` → `core.py:714-761` (build_claude_cmd)
   - `harness.py:652-663` → `session.py:54-73` (spawn_claude)
   - `harness.py:395-406` → `shell.py:88-93` (worksite_root)

2. **`dangerous_auto_approve` field doesn't exist** — config now uses
   `permissions.mode: dangerous` (string enum). Fixed in §1, §4.4 entrypoint,
   §6, and reference map.

3. **`config.yaml:41` reference was wrong** — updated to `config.yaml:35-48`
   (permissions block, shipped default `mode: safe`).

4. **Missing `--i-know-its-dangerous` CLI override** — added to §6.

5. **§12 CLI error handling is unimplemented** — added "proposed design" callout.

6. **`intent.md:108-109` reference map entry was wrong** — split into two correct
   entries (dangerous mode at 108-112, project-leader privilege at 115-119).

7. **Stale `harness.py` in limitations table** — fixed to `shell.py / session.py`.

## Subagents dispatched

- Haiku: read task folder (orientation)
- Haiku: read reference docs (dev-container.md, intent.md)
- Haiku: scan codebase structure (file list, core.py, shell.py, pyproject.toml)
- Haiku: verify all 5 code references (exact file:line mappings)
- Haiku: verify config.yaml structure, intent.md line refs, .gitattributes, retry fields
- Haiku: verify dangerous mode config field name and sandbox gating code
- Sonnet: apply all 8 edits to dev-container.md
- Haiku: verify revised doc (found 1 remaining harness.py ref → fixed inline)

## Decision

Single-role workflow — all work done via subagent delegation in this session.
Proceeding directly to wrap-up.
