---
name: feature-build
phases: [project-leader, brainstormer, plan-writer, developer, project-leader]
max_sessions: 25
---

# Feature-build workflow

Structured spec-driven loop with inline critique. The default linear order is:

1. **project-leader** — kickoff. Reads TASK.md, writes `00-kickoff.md`,
   confirms the workflow fits, and advances to brainstormer.
2. **brainstormer** — explores intent + requirements, writes
   `01-brainstorm.md`. Blocks the task if critical questions can't be
   answered without the user.
3. **plan-writer** — produces `02-plan.md` from the brief and brainstorm.
4. **developer** — executes the plan via subagent-driven development,
   writes `03-develop.md`, commits work in small atomic chunks. After all
   plan steps land and tests pass, the developer invokes the expert-critic
   subagent inline via the Agent tool (not as a separate session). Critique
   findings are synthesized into `03-develop.md`; CRITICAL findings loop
   the developer's own fix cycle before the session ends.
5. **project-leader** — wrap up. Writes `FINAL.md`, verifies clean git
   state, marks `status=done`.

## Loops

- project-leader → any earlier role: during review sessions, the
  project-leader can set `next_role` to loop any role back if the work
  has drifted.
- brainstormer → (user): if brainstormer blocks on a critical question,
  the task stalls until the user provides an answer (via CORRECTION.md
  or by editing STATE.status back to active).

Because `project-leader` appears at both ends of `phases`, the harness's
linear advance will land on it naturally for wrap-up.

## TASK.md extended fields

Tasks may include `depends_on`, `deliverables`, `refine_on_deps_complete`,
and `references` in their frontmatter, plus `## Success criteria`,
`## Boundaries`, `## Autonomy`, and `## Handoff` sections in the body.
All roles respect these fields — see each role's "Autonomy and boundaries"
section for details.
