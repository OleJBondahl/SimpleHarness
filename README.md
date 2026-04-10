# SimpleHarness

> **Status: Alpha** — usable, but expect rough edges. Clone it, try it, [open an issue](https://github.com/OleJBondahl/SimpleHarness/issues) with what breaks.

![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
![Status: Alpha](https://img.shields.io/badge/status-alpha-orange)

```
   ┌───────────────────────────────────────────────┐
   │            S I M P L E H A R N E S S          │
   │       Baton-pass agent harness for Claude Code │
   └───────────────────────────────────────────────┘

   [your roles] ──▶ [your workflow] ──▶ [your rules]
        │                                      │
        └── fresh context · state on disk ─────┘
```

Runs sequences of short, focused `claude -p` sessions instead of one long interactive session. Each session gets a fresh context window, a narrow role prompt, and reads/writes state to disk. You define the roles, workflows, and rules.

## Why this exists

Long Claude Code sessions degrade. Context fills up, the model drifts from the spec, and you end up babysitting. SimpleHarness splits work into a baton-pass of short sessions, each with a clean context window, a single role, and a clear brief.

It is a loop, not a framework. No SDK, no runtime. It spawns `claude -p`, reads a STATE.md file, and decides what to do next. Roles are markdown files. Workflows are markdown files. Configuration is YAML. You can read, edit, or replace any of it.

The seed roles (leader, brainstormer, planner, developer) are starting points. Write your own. Swap them out. Define workflows that match how you actually work.

Start it, walk away. Cost tracking, session caps, and no-progress detection keep things under control. Ctrl+C mid-session drops you into a correction prompt that gets injected into the next run. Or deploy in a dev container and don't touch it at all.

Fresh context per role means no accumulated noise. Phase file previews give each session just enough history. Less drift, lower cost, better output.

## Quick start

```bash
git clone https://github.com/OleJBondahl/SimpleHarness.git
cd SimpleHarness
uv sync                             # install dependencies

cd /your/project
simpleharness init                  # creates simpleharness/ directory
simpleharness new "add auth layer"  # creates a task
simpleharness watch                 # runs the baton-pass loop
```

## How it works

1. Scan `tasks/` for active tasks
2. Determine next role from workflow or `STATE.next_role`
3. Build prompt: preamble + TASK.md + phase history + corrections
4. Spawn `claude -p` with role's system prompt
5. Read updated STATE.md → advance or block
6. Loop until done (idle at 30s heartbeat)

`Ctrl+C` during a session kills the child process, drops you into a correction prompt, saves to CORRECTION.md, and resumes on the next tick.

## What you get

| | |
|---|---|
| Baton-pass sessions | Fresh context per role, no bloat accumulation |
| Custom roles | Markdown files. Write your own or use the seeds |
| Custom workflows | Define phase sequences. `universal` and `feature-build` included |
| Approver hook | Three permission modes: `safe`, `approver`, `dangerous` |
| Cost tracking | Per-session and per-task USD cost and duration |
| Task dependencies | `depends_on`, deliverables with `min_lines` verification |
| Pause / resume | `.PAUSE` signal file or CLI commands |
| Mid-flight steering | Ctrl+C, correction prompt, injected next session |
| Rich dashboard | `simpleharness status` shows progress, cost, blocked reasons |
| Dev container ready | Unattended runs with sandbox detection |

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

- `safe` (default): `--permission-mode acceptEdits` with a curated Bash allowlist
- `approver`: two-layer hook. Fast Bash pattern match first, then a Sonnet approver session on cache miss
- `dangerous`: `bypassPermissions`, dev containers only (`doctor` enforces sandbox)

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

## Writing custom roles

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

## Writing custom workflows

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
- No API keys needed. Claude Code handles authentication.

## Skills

SimpleHarness roles reference [Claude Code skills](https://github.com/OleJBondahl/claude-skills) for documentation writing, code review, debugging, and more. To use the same skill set:

```bash
git clone https://github.com/OleJBondahl/claude-skills.git ~/.claude/skills
```

Or copy individual skill folders from the repo into `~/.claude/skills/`.

[docs/usage.md](docs/usage.md) has the full reference: TASK.md schema, directory layout, and configuration details.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, code style, and PR expectations.

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for what's shipped so far.

## License

MIT
