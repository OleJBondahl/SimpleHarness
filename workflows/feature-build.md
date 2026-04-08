---
name: feature-build
phases: [project-leader, brainstormer, plan-writer, developer, expert-critic, project-leader]
max_sessions: 25
---

# Feature-build workflow

Structured spec-driven loop with critique. The default linear order is:

1. **project-leader** — kickoff. Reads TASK.md, writes `00-kickoff.md`,
   confirms the workflow fits, and advances to brainstormer.
2. **brainstormer** — explores intent + requirements, writes
   `01-brainstorm.md`. Blocks the task if critical questions can't be
   answered without the user.
3. **plan-writer** — produces `02-plan.md` from the brief and brainstorm.
4. **developer** — executes the plan via subagent-driven development,
   writes `03-develop.md`, commits work in small atomic chunks.
5. **expert-critic** — reviews with a specific expert area in mind (the
   project-leader may dispatch multiple critics by setting `next_role`
   back to expert-critic with a different expert_area brief).
6. **project-leader** — wrap up. Writes `FINAL.md`, verifies clean git
   state, marks `status=done`.

## Loops

- expert-critic → developer: if the critic finds CRITICAL issues, it sets
  `next_role=developer` to loop back. The same-role-repetition cap (3)
  prevents infinite flip-flops.
- project-leader → any earlier role: during review sessions, the
  project-leader can set `next_role` to loop any role back if the work
  has drifted.
- brainstormer → (user): if brainstormer blocks on a critical question,
  the task stalls until the user provides an answer (via CORRECTION.md
  or by editing STATE.status back to active).

Because `project-leader` appears at both ends of `phases`, the harness's
linear advance will land on it naturally for wrap-up.
