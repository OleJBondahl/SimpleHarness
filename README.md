# SimpleHarness

A lightweight Python harness that runs **baton-pass agent workflows** over the
Claude Code CLI. Long, drifting sessions become short, focused role-phased
sessions that pass state between each other via markdown files on disk. You
start it in a worksite, walk away, and come back when it's done.

## Install

```bash
uv tool install -e C:/Users/OleJohanBondahl/Documents/Github_OJ/SimpleHarness
```

`simpleharness` is now on your PATH from any directory.

For development on this repo, also run once:

```bash
uvx pre-commit install   # enables ruff + ty pre-commit hook
```

## Quick start

```bash
cd C:/your/project                        # any git repo
simpleharness init                        # one-time, creates simpleharness/ folder
simpleharness new "add a CHANGELOG entry for v0.2" --workflow=universal
simpleharness watch                       # long-lived loop, passes the baton until done
```

Hit `Ctrl+C` during a session to steer: the harness kills the current session,
prompts you in the terminal, and whatever you type (end with a blank line) is
written to `CORRECTION.md` and injected into the next session's prompt.
Press `Ctrl+C` a second time at the correction prompt to abort the harness.

## What's happening

1. `init` creates a `simpleharness/` folder in your worksite with `tasks/`,
   `memory/`, and `logs/` subfolders.
2. `new` scaffolds a task folder: `simpleharness/tasks/NNN-slug/` containing
   `TASK.md` (your brief) and `STATE.md` (the harness pointer).
3. `watch` runs a long-lived loop. On each tick it scans `tasks/` for any task
   with `status=active`, picks the next active one, figures out which role
   should run next (from the workflow file or `STATE.next_role`), and spawns a
   headless `claude -p` subprocess with that role's system prompt.
4. Each session writes its phase file (`00-kickoff.md`, `01-brainstorm.md`,
   etc.), updates `STATE.md`, and commits any worksite changes it made.
5. When the final role writes `FINAL.md` and sets `status=done`, the harness
   moves on to the next active task — or goes idle if there are none.

## Toolbox vs Worksite

SimpleHarness uses a two-repo split:

- **Toolbox** (this repo): holds `harness.py`, `roles/*.md`, `workflows/*.md`,
  and `config.yaml`. Installed once globally. Contains the "brain".
- **Worksite**: any git repo you `cd` into and run `simpleharness` from. All
  task state, logs, memory, and corrections live inside that worksite's own
  `simpleharness/` folder. Running multiple worksites is safe — each has its
  own state.

The project-leader role is allowed to edit files in the toolbox so it can
improve its own role/workflow definitions over time. If you're running multiple
SimpleHarness instances concurrently, only instruct one of them to do
toolbox-level work at a time to avoid commit conflicts.

## Commands

- `simpleharness watch` — long-lived loop (primary mode)
- `simpleharness watch --once` — single tick, useful for debugging
- `simpleharness new "<title>" --workflow=<name>` — scaffold a new task
- `simpleharness init` — create `simpleharness/` folder layout in current dir
- `simpleharness status` — list active tasks + their current phase
- `simpleharness list` — list all tasks (active + done)
- `simpleharness show <task-slug>` — summarize a task folder
- `simpleharness doctor` — sanity checks (claude on PATH, toolbox reachable, etc.)
- `simpleharness --version`

## Configuration

Defaults live in `config.yaml` at the toolbox root. Per-worksite overrides
go in `<worksite>/simpleharness/config.yaml` (only the fields you want to
change; the rest fall through).

Key fields:

- `model: opus` — base model for all roles
- `idle_sleep_seconds: 30` — heartbeat idle cadence
- `max_sessions_per_task: 20` — hard cap per task
- `permissions.dangerous_auto_approve: false` — safe default

## Permissions: safe vs dangerous mode

By default (**safe mode**), the harness passes `--permission-mode acceptEdits`
plus a curated `--allowedTools` list covering file edits, reads, git, uv, npm,
pytest, ruff, and a few other safe patterns. Bash commands outside the
allowlist **fail cleanly** — the agent sees the failure and reports it in its
phase file.

Set `permissions.dangerous_auto_approve: true` in `config.yaml` to switch to
`--permission-mode bypassPermissions`, which approves everything. **Only do
this in a dev container, VM, or disposable WSL** — `simpleharness doctor`
will refuse to start `watch` unless it detects a sandbox marker (`/.dockerenv`
or the `SIMPLEHARNESS_SANDBOX=1` env var), or you pass
`--i-know-its-dangerous`.

To extend the allowlist without going dangerous, add patterns to
`permissions.extra_bash_allow` in `config.yaml`.

## Directory layout

### Toolbox (this repo)

```
SimpleHarness/
├── intent.md                         # the original vision
├── pyproject.toml                    # uv-managed, Python 3.13
├── README.md                         # this file
├── harness.py                        # the tool
├── config.yaml                       # toolbox defaults
├── roles/                            # role definitions (markdown)
│   ├── project-leader.md
│   ├── brainstormer.md
│   ├── plan-writer.md
│   └── developer.md
├── subagents/                        # subagent definitions (Claude Code .claude/agents format)
│   └── expert-critic.md
├── workflows/
│   ├── universal.md
│   └── feature-build.md
└── .claude/
    └── settings.json                 # permissions allowlist for interactive debug
```

### Worksite runtime folder (per project)

```
<your-worksite>/
├── (your code / project files)
└── simpleharness/
    ├── config.yaml                   # optional per-worksite overrides
    ├── tasks/
    │   └── 001-example-task/
    │       ├── TASK.md               # you write this
    │       ├── STATE.md              # harness pointer
    │       ├── 00-kickoff.md         # project-leader's notes
    │       ├── 01-brainstorm.md
    │       ├── ... (phase files)
    │       └── FINAL.md              # written by last role
    ├── memory/
    │   └── WORKSITE.md               # long-term notes
    └── logs/
        └── 001-example-task/
            └── 00-project-leader.jsonl
```

## Status

v0.1 MVP — single-tick `watch --once`, init, new, minimal role library.
See `docs/` or the plan file for the full roadmap.
