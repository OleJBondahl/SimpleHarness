# SimpleHarness

```
   ┌───────────────────────────────────────────────┐
   │            S I M P L E H A R N E S S          │
   │       Baton-pass agent harness for Claude Code │
   └───────────────────────────────────────────────┘

   [your roles] ──▶ [your workflow] ──▶ [your rules]
        │                                      │
        └── fresh context · state on disk ─────┘
```

A lightweight harness that runs sequences of short, focused `claude -p` sessions instead of one long interactive session. Each session gets a fresh context window, a narrow role-specific prompt, and reads/writes state to disk. You define the roles, workflows, and rules.

## Philosophy

**Long Claude Code sessions degrade.** Context fills up, the model drifts from the spec, and you end up babysitting. SimpleHarness fixes this by splitting work into a baton-pass of short sessions — each with a clean context window, a single role, and a clear brief.

**Thin wrapper, not a framework.** No SDK. No runtime. No external dependencies beyond Python and PyYAML. SimpleHarness is a loop that spawns `claude -p`, reads a STATE.md file, and decides what to do next. Roles are markdown files. Workflows are markdown files. Configuration is YAML. Everything is readable, editable, and replaceable.

**Bring your own everything.** The seed roles (leader, brainstormer, planner, developer) are starting points. Write your own. Swap them out. Define workflows that match how you actually work — not how a framework thinks you should.

**Run unattended.** Start it, walk away. Cost tracking, session caps, and no-progress detection keep things under control. Hit Ctrl+C to course-correct mid-flight — your correction gets injected into the next session's prompt. Deploy in a dev container for fully hands-off operation.

**Context efficiency is the feature.** Every token matters when you're paying per session. Fresh context per role means no accumulated noise. Phase file previews give each session just enough history. The result: less drift, lower cost, better output.

## Quick Start

```bash
pip install simpleharness          # or: uv add simpleharness

cd /your/project
simpleharness init                  # creates simpleharness/ directory
simpleharness new "add auth layer"  # creates a task
simpleharness watch                 # runs the baton-pass loop
```

## How It Works

1. Scan `tasks/` for active tasks
2. Determine next role from workflow or `STATE.next_role`
3. Build prompt: preamble + TASK.md + phase history + corrections
4. Spawn `claude -p` with role's system prompt
5. Read updated STATE.md → advance or block
6. Loop until done (idle at 30s heartbeat)

Mid-flight steering: `Ctrl+C` during a session kills the child process, drops you into a correction prompt, saves to CORRECTION.md, and resumes on the next tick.

## Features

| Feature | What it does |
|---|---|
| **Baton-pass sessions** | Fresh context per role. No bloat accumulation. |
| **Custom roles** | Markdown files. Write your own or use the seeds. |
| **Custom workflows** | Define phase sequences. `universal` or `feature-build` included. |
| **Approver hook** | Three permission modes: `safe`, `approver`, `dangerous`. |
| **Cost tracking** | Per-session and per-task USD cost + duration. |
| **Task dependencies** | `depends_on`, deliverables with `min_lines` verification. |
| **Pause / resume** | `.PAUSE` signal file or CLI commands. |
| **Mid-flight steering** | Ctrl+C → correction prompt → injected next session. |
| **Rich dashboard** | `simpleharness status` — progress, cost, blocked reasons. |
| **Dev container ready** | Unattended runs with sandbox detection. |

## Commands

| Command | Description |
|---|---|
| `simpleharness init` | Scaffold a worksite in the current project |
| `simpleharness new "description"` | Create a new task |
| `simpleharness watch` | Run the main loop |
| `simpleharness status` | Rich dashboard of all tasks |
| `simpleharness list` | List tasks |
| `simpleharness show <slug>` | Show task details |
| `simpleharness pause` | Pause the harness |
| `simpleharness resume` | Resume after pause |
| `simpleharness doctor` | Verify environment and permissions |

## Configuration

Edit `simpleharness/config.yaml` in your worksite:

```yaml
model: claude-sonnet-4-20250514
idle_sleep: 30
session_cap: 20
permissions: safe              # safe | approver | dangerous
skills:
  - python-coding-and-tooling
```

## Permissions

- **`safe`** (default) — `--permission-mode acceptEdits` + curated Bash allowlist
- **`approver`** — two-layer hook: fast Bash pattern match → Sonnet approver session on cache miss
- **`dangerous`** — `bypassPermissions`, dev containers only (`doctor` enforces sandbox)

## Architecture

```
your-project/
├── simpleharness/           # worksite (created by init)
│   ├── config.yaml
│   ├── tasks/
│   │   └── add-auth-layer/
│   │       ├── TASK.md
│   │       ├── STATE.md
│   │       └── phases/
│   └── .approver-allowlist.txt
│
~/.simpleharness/            # toolbox (shared across projects)
├── roles/
│   ├── project-leader.md
│   ├── brainstormer.md
│   ├── plan-writer.md
│   └── developer.md
├── workflows/
│   ├── universal.md
│   └── feature-build.md
└── config.yaml
```

## Writing Custom Roles

A role is a markdown file in `roles/`. It becomes the system prompt for that session.

```markdown
# roles/my-reviewer.md
---
name: reviewer
model: claude-sonnet-4-20250514
---

You are a code reviewer. Review the changes described in TASK.md.
Focus on: security, correctness, test coverage.
Write your findings to phases/review.md.
```

## Writing Custom Workflows

A workflow defines the sequence of roles:

```markdown
# workflows/review-cycle.md
---
name: review-cycle
phases:
  - developer
  - reviewer
  - developer
---
```

## Requirements

- Python ≥ 3.13
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated
- No API keys.

See [docs/usage.md](docs/usage.md) for detailed usage reference, TASK.md schema, and directory layout.

## License

MIT
