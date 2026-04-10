---
name: feature-build-local
phases: [project-leader, brainstormer, plan-writer, local-worker, project-leader]
max_sessions: 25
---

# Feature-build-local workflow

Cost-optimised variant of feature-build. Planning phases use Claude (Opus
subscription), execution runs on a local Ollama model (Qwen3.5 9B, free).

1. **project-leader** (Opus) — kickoff. Reads TASK.md, writes `00-kickoff.md`,
   confirms the workflow fits, and advances to brainstormer.
2. **brainstormer** (Opus) — explores intent + requirements, writes
   `01-brainstorm.md`. Blocks the task if critical questions can't be
   answered without the user.
3. **plan-writer** (Opus) — produces `02-plan.md` with step-by-step
   instructions detailed enough for the local model to follow.
   **Important:** plan steps must be self-contained and explicit — the
   local model cannot infer context or make judgment calls.
4. **local-worker** (Ollama) — executes the plan step by step. Writes
   `03-develop.md`. Runs lint and tests after each change. If stuck or
   if a step requires complex reasoning, sets `next_role: developer` to
   escalate back to Opus.
5. **project-leader** (Opus) — wrap up. Reviews local-worker output,
   fixes anything that needs Opus-level judgment, writes `FINAL.md`,
   marks `status=done`.

## Cost profile

- Phases 1-3 + 5: ~4 short Opus sessions (kickoff, brainstorm, plan, review)
- Phase 4: 1+ Ollama sessions (the bulk of the work) — FREE
- Estimated savings vs feature-build: 60-70% fewer subscription tokens

## Key difference from feature-build

The plan-writer must produce **more detailed plans** than usual. Each step
should specify exact file paths, line ranges, and what to write — because
the local model has a small context window and limited reasoning. Think of
the plan as instructions for a competent but literal junior developer.

## Escalation

The local-worker can escalate any step back to Opus by setting
`next_role: developer` in STATE.md. The harness will spawn a regular
developer session for that step, then return to the local-worker for
remaining steps.
