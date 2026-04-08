# Skills for the SimpleHarness workflow

Skills that the SimpleHarness-driven workflow should invoke when it is actually **developing and building things for the user** — mapped to the role files in `roles/` and the workflow files in `workflows/`.

This file is the authoritative reference for "which skill does which role pull in." When editing a role file, update the mapping here too.

---

## A. Role → skill mapping

Aligned with files currently in `roles/`.

| Role file | Primary skills | Secondary skills |
|---|---|---|
| `roles/brainstormer.md` | `brainstorming`, `jobs-to-be-done`, `problem-framing-canvas` | `opportunity-solution-tree`, `expert-panel` |
| `roles/plan-writer.md` | `writing-plans` | `brainstorming` (if scope unclear), `using-git-worktrees` |
| `roles/developer.md` | `test-driven-development`, `executing-plans`, `subagent-driven-development` | `systematic-debugging`, `haiku-delegate`, `dispatching-parallel-agents`, `claude-api` (if SDK work) |
| `roles/expert-critic.md` | `receiving-code-review`, `expert-panel` | `verification-before-completion`, `systematic-debugging` |
| `roles/approver.md` | *(no skill — harness approval logic)* | `update-config`, `hookify/writing-rules` (as design reference only) |
| `roles/project-leader.md` | `roadmap-planning`, `finishing-a-development-branch` | `requesting-code-review`, `updating-memory` |

---

## B. Workflow phase → skill mapping

Aligned with `workflows/feature-build.md` and `workflows/universal.md`.

| Phase | Skills |
|---|---|
| Discovery / intent | `brainstorming`, `jobs-to-be-done`, `problem-framing-canvas` |
| Planning | `writing-plans`, `using-git-worktrees` |
| Implementation | `test-driven-development`, `executing-plans`, `subagent-driven-development`, `haiku-delegate`, `dispatching-parallel-agents` |
| Debugging | `systematic-debugging` |
| Review | `requesting-code-review`, `receiving-code-review`, `expert-panel` |
| Verification | `verification-before-completion` |
| Completion | `finishing-a-development-branch`, `updating-memory` |
| SDK / API work (any phase) | `claude-api` |
| Library docs lookup (any phase) | `claude-api` + `context7` MCP |

---

## C. Cross-cutting skills (used in any phase, by any role)

- **`haiku-delegate`** — mandatory for mechanical work (file search, code reading, command execution with verbose output). Per the global CLAUDE.md, this is non-optional.
- **`updating-memory`** — after any task that changes code, adds functions, modifies APIs, or reveals non-obvious insights.
- **`verification-before-completion`** — before claiming "done", committing, or creating a PR. Evidence before assertions.

---

## D. Recommended new roles to add to `roles/`

The skills below cover surfaces the current role set doesn't address (docs, deploy, strategy). They are proposals — *creating these role files is a separate task.* Each new role should mirror the structure of existing `roles/developer.md` etc.

### D.1 `documentation-writer.md` *(proposed)*

Writes user-facing docs, README sections, release notes, tutorials, and long-form guides.

- **Primary**
  - `humanizer` — strip AI-voice tells from generated prose (the explicit purpose of this skill)
  - `python-skills/documentation` — docstring conventions, Sphinx / MkDocs patterns
  - `apa-citations` — only for formal technical documents that need reference lists
- **Secondary**
  - `visual-review` — render-and-read loop when docs include diagrams
  - `claude-md-improver` — when docs overlap with `CLAUDE.md` content
  - `writing-skills` — when authoring a new reusable skill document

### D.2 `deployment-engineer.md` *(proposed)*

Owns releases, remote deploys, server provisioning, health checks, and rollback.

- **Primary**
  - `deploy-remote` — full build → transfer → restart → verify pipeline for remote hosts
  - `ssh-remote` — running commands on remote machines and WSL (ControlMaster, file transfer)
  - `python-skills/release-management` — versioning, changelogs, PyPI release flow
- **Secondary**
  - `python-skills/packaging` — wheel / sdist / entry point correctness
  - `verification-before-completion` — health checks after deploy, evidence for rollback decisions
  - `systematic-debugging` — triaging deploy failures

### D.3 `ceo.md` *(proposed — product / strategy lead)*

Owns direction, positioning, prioritization, and stakeholder framing. Invoked for **"should we build X"** questions, not **"how do we build X"**.

- **Primary**
  - `product-strategy-session` — end-to-end strategy run across positioning, discovery, and roadmap
  - `jobs-to-be-done` — customer jobs / pains / gains in a structured JTBD format
  - `positioning-statement` — Geoffrey Moore-style positioning (who, problem, category, differentiator)
  - `roadmap-planning` — turning strategy into a sequenced release plan
- **Secondary**
  - `problem-framing-canvas` — MITRE-style framing before solutioning
  - `opportunity-solution-tree` — outcomes → opportunities → solutions → tests
  - `expert-panel` — multi-constraint tradeoff calls (security vs. cost vs. UX)

---

## Maintenance

When a role or workflow file changes:
1. Update the mapping table in Section A or B above.
2. If a new skill becomes relevant, add it to the table rather than creating a parallel list.
3. If a proposed role in Section D gets created as a real file in `roles/`, move it up into Section A and delete the proposal entry.
