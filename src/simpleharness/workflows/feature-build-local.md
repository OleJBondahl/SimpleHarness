---
name: feature-build-local
phases: [project-leader, brainstormer, plan-writer, local-worker, project-leader]
max_sessions: 25
---

# Feature-build-local workflow

Cost-optimised variant of feature-build. Planning phases use Claude (Opus
subscription), execution runs on Haiku (fast and cheap).

1. **project-leader** (Opus) — kickoff. Reads TASK.md, writes `00-kickoff.md`,
   confirms the workflow fits, and advances to brainstormer.
2. **brainstormer** (Opus) — explores intent + requirements, writes
   `01-brainstorm.md`. Blocks the task if critical questions can't be
   answered without the user.
3. **plan-writer** (Opus) — produces `02-plan.md` with step-by-step
   instructions detailed enough for Haiku to follow.
   **Important:** plan steps must be self-contained and explicit.
4. **local-worker** (Haiku) — executes the plan step by step. Writes
   `03-develop.md`. Runs lint and tests after each change. If stuck or
   if a step requires complex reasoning, sets `next_role: developer` to
   escalate back to Opus.
5. **project-leader** (Opus) — wrap up. Reviews local-worker output,
   fixes anything that needs Opus-level judgment, writes `FINAL.md`,
   marks `status=done`.

## Cost profile

- Phases 1-3 + 5: ~4 short Opus sessions (kickoff, brainstorm, plan, review)
- Phase 4: 1+ Haiku sessions (the bulk of the work) — very cheap
- Estimated savings vs feature-build: 60-70% fewer Opus tokens

## Key difference from feature-build

The plan-writer should produce clear, self-contained steps. Haiku is capable
but works best with explicit acceptance criteria and focused scope. Think of
the plan as instructions for a fast, reliable junior developer.

## Escalation

The local-worker can escalate any step back to Opus by setting
`next_role: developer` in STATE.md. The harness will spawn a regular
developer session for that step, then return to the local-worker for
remaining steps.
