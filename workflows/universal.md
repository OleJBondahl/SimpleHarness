---
name: universal
phases: [project-leader]
max_sessions: 20
---

# Universal workflow

One-phase workflow. The project-leader role runs every session and decides
dynamically — via `STATE.next_role` — which other role to dispatch to next,
or whether the task is complete.

Best for:
- Exploratory or unclear tasks where a fixed phase order doesn't fit
- Small tasks where a full 5-phase chain would be overkill
- Testing the harness itself (the MVP smoke test uses this workflow)

Expected flow (examples, not rules):
- Session 1: project-leader kicks off, writes `00-kickoff.md`, sets
  `next_role` to whichever role fits the task (e.g., `brainstormer` for
  fuzzy requirements, `plan-writer` for a clear brief, or `developer`
  directly for a trivial fix).
- Subsequent sessions: the dispatched role runs; project-leader reviews,
  loops back, or wraps up. (Expert review is done inline by the developer via the expert-critic subagent.)
- Final session: project-leader writes `FINAL.md`, sets `status=done`.

Since `phases` contains only `project-leader`, the harness's default
phase-advance logic will never naturally pick another role — every
non-project-leader session happens because project-leader set
`STATE.next_role` explicitly.

## TASK.md extended fields

Tasks may include `depends_on`, `deliverables`, `refine_on_deps_complete`,
and `references` in their frontmatter, plus `## Success criteria`,
`## Boundaries`, `## Autonomy`, and `## Handoff` sections in the body.
All roles respect these fields — see each role's "Autonomy and boundaries"
section for details.
