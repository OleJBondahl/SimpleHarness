# Skills for the SimpleHarness workflow

Skills that the SimpleHarness-driven workflow should invoke when it is actually **developing and building things for the user** — mapped to the role files in `src/simpleharness/roles/` and the workflow files in `src/simpleharness/workflows/`.

This file is the authoritative reference for "which skill does which role pull in." When editing a role file, update the mapping here too.

---

## Global defaults (`config.yaml`)

These are merged into every role's `skills:` frontmatter via `merge_skill_lists`. Roles can opt out of a specific default with `exclude_default_must_use` in their frontmatter.

| Field | Skills | Effect |
|---|---|---|
| `default_available` | `haiku-delegate`, `python-coding-and-tooling` | Every role sees these in its SessionStart reminder. |
| `default_must_use` | `updating-memory` | Every role must invoke this before stopping (enforced by Stop hook in `strict` mode). |

**Why these are global:**
- `haiku-delegate` — CLAUDE.md mandates delegating mechanical work to Haiku subagents. Non-optional for all roles.
- `python-coding-and-tooling` — CLAUDE.md mandates this for all Python work in this repo.
- `updating-memory` — ensures knowledge persistence across sessions. Every role that runs a full session should update the MCP memory server.

---

## A. Role → skill mapping

Aligned with files in `src/simpleharness/roles/`. Skills listed here match the `skills:` frontmatter in each role file.

| Role file | `must_use` | `available` |
|---|---|---|
| `brainstormer.md` | `brainstorming` | brainstorming, jobs-to-be-done, problem-framing-canvas, opportunity-solution-tree, expert-panel |
| `plan-writer.md` | `writing-plans` | writing-plans, using-git-worktrees, brainstorming, codebase-memory-exploring |
| `developer.md` | `verification-before-completion` | executing-plans, subagent-driven-development, test-driven-development, systematic-debugging, dispatching-parallel-agents, verification-before-completion, commit-commands:commit, finishing-a-development-branch |
| `project-leader.md` | *(none)* | roadmap-planning, requesting-code-review, finishing-a-development-branch, claude-md-management:claude-md-improver, codebase-memory-exploring |
| `approver.md` | *(none)*, excludes: `updating-memory` | *(none)* |
| `documentation-writer.md` | `humanizer` | humanizer, apa-citations, visual-review, claude-md-management:claude-md-improver, writing-skills, verification-before-completion |
| `deployment-engineer.md` | `verification-before-completion` | deploy-remote, ssh-remote, systematic-debugging, verification-before-completion, finishing-a-development-branch, commit-commands:commit |
| `ceo.md` | *(none)* | product-strategy-session, jobs-to-be-done, positioning-statement, roadmap-planning, problem-framing-canvas, opportunity-solution-tree, expert-panel |

**Effective `must_use` after merge with global defaults:**

| Role | Effective `must_use` |
|---|---|
| brainstormer | `updating-memory`, `brainstorming` |
| plan-writer | `updating-memory`, `writing-plans` |
| developer | `updating-memory`, `verification-before-completion` |
| project-leader | `updating-memory` |
| approver | *(none — opted out of updating-memory)* |
| documentation-writer | `updating-memory`, `humanizer` |
| deployment-engineer | `updating-memory`, `verification-before-completion` |
| ceo | `updating-memory` |

## A2. Subagent → skill mapping

Aligned with files in `src/simpleharness/subagents/`. These are invoked inline via the Agent tool, not as separate workflow phases.

| Subagent file | `must_use` | `available` |
|---|---|---|
| `expert-critic.md` | *(none)* | expert-panel, systematic-debugging, receiving-code-review |

---

## B. Workflow phase → skill mapping

Aligned with `workflows/feature-build.md` and `workflows/universal.md`.

| Phase | Skills |
|---|---|
| Strategy / direction | `product-strategy-session`, `jobs-to-be-done`, `positioning-statement`, `roadmap-planning`, `problem-framing-canvas`, `opportunity-solution-tree`, `expert-panel` |
| Discovery / intent | `brainstorming`, `jobs-to-be-done`, `problem-framing-canvas` |
| Planning | `writing-plans`, `using-git-worktrees`, `codebase-memory-exploring` |
| Implementation | `test-driven-development`, `executing-plans`, `subagent-driven-development`, `dispatching-parallel-agents` |
| Debugging | `systematic-debugging` |
| Review | `requesting-code-review`, `receiving-code-review`, `expert-panel` |
| Documentation | `humanizer`, `apa-citations`, `visual-review`, `writing-skills` |
| Deployment | `deploy-remote`, `ssh-remote` |
| Verification | `verification-before-completion` |
| Completion | `finishing-a-development-branch`, `commit-commands:commit`, `updating-memory` |
| SDK / API work (any phase) | `claude-api` |
| Library docs lookup (any phase) | `claude-api` + `context7` MCP |

---

## C. Cross-cutting skills (used in any phase, by any role)

- **`haiku-delegate`** — mandatory for mechanical work (file search, code reading, command execution). Per the global CLAUDE.md, this is non-optional. Now a global default in `config.yaml`.
- **`python-coding-and-tooling`** — Python toolchain conventions for this repo. Now a global default in `config.yaml`.
- **`updating-memory`** — after any task that changes code, adds functions, modifies APIs, or reveals non-obvious insights. Global `must_use` default.
- **`verification-before-completion`** — before claiming "done", committing, or creating a PR. Evidence before assertions.

---

## D. Design rationale

### `must_use` philosophy

Only 4 of 8 roles have role-specific `must_use` skills. Each represents the core protocol that **defines** the role:

| Role | `must_use` | Why it's mandatory |
|---|---|---|
| brainstormer | `brainstorming` | The structured brainstorming protocol IS the job. |
| plan-writer | `writing-plans` | The structured plan-writing protocol IS the job. |
| developer | `verification-before-completion` | Unverified code must not ship. Safety gate. |
| documentation-writer | `humanizer` | AI-voice in user-facing docs is a quality defect. |
| deployment-engineer | `verification-before-completion` | Unverified deploys are dangerous. Safety gate. |

Roles with no role-specific `must_use` (project-leader, ceo) have sessions that are too variable for any single skill to apply every time.

### Approver exclusion

The approver is a 3-turn, non-workflow micro-role invoked by the MCP permission handler. Forcing `updating-memory` would waste one of its three turns on a memory update with nothing meaningful to record. It opts out via `exclude_default_must_use: [updating-memory]`.

---

## Maintenance

When a role, subagent, or workflow file changes:
1. Update the mapping table in Section A, A2, or B above.
2. If a new skill becomes relevant, add it to the table rather than creating a parallel list.
3. If a new subagent is added to `src/simpleharness/subagents/`, add it to Section A2.
4. If global defaults in `config.yaml` change, update the Global defaults section.
