# SimpleHarness — Project Intent

> A thin Python harness that turns one bloated `claude` session into a chain of focused, role-specialized sessions that pass state through markdown files on disk. Start it from any project, walk away, come back when it's done.

## What it is

SimpleHarness is a single-file Python tool (`harness.py`) that wraps the Claude Code CLI in headless mode (`claude -p`). Instead of running one long interactive session that bloats with context and drifts from the plan, you give SimpleHarness a markdown brief and it works on your task across however many short, fresh sessions are needed — each with a different specialized role — until the work is done, the worksite is committed, and a `FINAL.md` report is on disk.

It is intentionally tiny: ~800 lines of Python, two third-party dependencies (`pyyaml`, `rich`), and a markdown library of roles + workflows that you can edit by hand. Everything that matters lives in markdown files, not in code.

## Why it exists

Three problems with running Claude Code interactively for non-trivial work:

1. **Context bloat.** Long sessions accumulate conversation history that pollutes the agent's reasoning. By the time you're 50 turns in, the agent has forgotten its plan, mixed up file contents, and is acting on stale assumptions.
2. **Drift without a steering wheel.** When an agent goes sideways mid-session, your only options are to interrupt and start over (losing context) or let it complete and clean up.
3. **Single-session ceiling.** Any task that exceeds one productive context window has no clean way to continue. Picking up where you left off means re-explaining everything.

SimpleHarness fixes all three:
- Each phase is a fresh `claude -p` invocation with a clean context window and a narrow brief.
- `Ctrl+C` kills the current session and drops you straight into a terminal correction prompt — whatever you type becomes the first thing the next session reads.
- State passes between sessions through markdown files on disk, so a task can span dozens of sessions without ever re-explaining itself.

## The core mental model

### Two-repo split

- **Toolbox** (this repo, `SimpleHarness/`) — the brain. Holds `harness.py`, the role library, the workflow library, default config. Installed once globally with `uv tool install -e .`.
- **Worksite** (any project) — the hands. Any git repo where you want work done. SimpleHarness creates a `simpleharness/` folder inside it for per-worksite task state, memory, and logs.

The toolbox is shared across all your projects. Each worksite has its own isolated task history. You can run multiple SimpleHarness instances in parallel against different worksites — they don't see each other. The only shared resource is the toolbox repo itself; if you ask one running instance to edit toolbox files (see "Self-improvement" below), don't ask another at the same time.

### The baton pass

A task moves through phases. Each phase is one fresh `claude -p` session with:

- A specific role loaded via `--append-system-prompt-file <toolbox>/roles/<role>.md`
- The worksite directory as `cwd`
- The toolbox added via `--add-dir` so the session can read role/workflow files
- A pre-built prompt assembled from a spatial-awareness preamble, the user's `TASK.md`, any prior phase files, and (if present) `CORRECTION.md`
- A curated permission allowlist (or full bypass in opt-in dangerous mode)

When the session ends, the harness reads the updated `STATE.md`, decides which role runs next (the workflow's default order, OR whatever the previous role wrote into `STATE.next_role`), and starts the next session. **State passes via files on disk, not via conversation history.** Each role gets a clean context window, hands off via STATE + phase file, then exits.

This is the "baton pass": no role ever sees what another role's session looked like internally — only the artifacts they wrote down.

### The five seed roles

Each role is a markdown file with YAML frontmatter + a system-prompt body. The harness loads the body via `--append-system-prompt-file`, so it gets appended to Claude Code's default system prompt rather than replacing it.

| Role | Purpose | Maps to | Special |
|---|---|---|---|
| **project-leader** | Orchestrator and meta-role. Runs at the start of every task to decide what happens next, and at the end to write `FINAL.md` and verify clean git state. | — (unique to SimpleHarness) | Privileged — the only role allowed to edit toolbox files |
| **brainstormer** | Explores user intent, surfaces clarifying questions. Used when a brief is fuzzy. | `superpowers:brainstorming` | |
| **plan-writer** | Produces a concrete, executable implementation plan. | `superpowers:writing-plans` | |
| **developer** | Executes the plan via subagent-driven development — dispatches Sonnet subagents per plan step, synthesizes results, commits in atomic chunks. | `superpowers:subagent-driven-development` | |
| **expert-critic** | Reviews prior work from a specific expert angle (security, a11y, performance, etc.) supplied per invocation. | `superpowers:expert-panel` (single-expert variant) | Often called multiple times per task with different expert areas |

All roles run on **Opus** because judgment matters most. To keep cost and context manageable, every role's body explicitly tells the agent to **dispatch Haiku and Sonnet subagents** for mechanical work (file scans, test runs, git inspection, scoped implementation steps). The Opus context is reserved for synthesis, decisions, and orchestration. This subagent-delegation pattern is the single biggest context-efficiency lever the harness gets, and it costs zero Python code — it lives in the role markdown bodies.

### Workflows

A workflow file is a thin shell that lists the default role order for a task type. Two ship by default:

- **`workflows/universal.md`** — single-phase: just `[project-leader]`. The project-leader picks every other role dynamically via `STATE.next_role`. Best for exploratory or ambiguous work where a fixed order doesn't fit.
- **`workflows/feature-build.md`** — six phases: `project-leader → brainstormer → plan-writer → developer → expert-critic → project-leader`. The structured spec-driven loop. The project-leader appears at both ends so the harness's linear advance lands on it for wrap-up.

Roles can override the default order by writing `next_role` into `STATE.md` mid-task. The expert-critic can loop back to the developer if it finds critical issues. The brainstormer can block the task if it needs user input. The harness has a same-role-3x cap to prevent infinite flip-flops, plus a hard per-task session cap (default 20) and a no-progress hash detector that catches stalled tasks.

Adding a new workflow is just dropping a markdown file into `workflows/`. No Python changes.

### The intervention model

The whole tool is built around the assumption that you'll need to steer mid-flight. That's the "harness" in SimpleHarness.

While a session is running, the live `stream-json` output renders to your terminal via `rich`. If the agent goes off course:

1. Hit **`Ctrl+C` once.** The harness terminates the child `claude` process cleanly (Windows-aware via `CREATE_NEW_PROCESS_GROUP` so the parent keeps control).
2. The harness drops to a correction prompt: *"Type your correction. Blank line + Enter = save & resume. Ctrl+C again = abort."*
3. **Type whatever you want** — multiple lines OK. End with a blank line.
4. The harness writes everything you typed to `<task>/CORRECTION.md`, then resumes its loop.
5. The next session (same role retrying, by default) reads `CORRECTION.md` as the first thing in its prompt under a loud `## USER INTERVENTION — READ THIS FIRST` header, and follows your guidance. The file is **deleted immediately after the prompt is built**, so each correction is consumed exactly once.

**A second `Ctrl+C` during the correction prompt aborts the harness entirely.**

If the agent is fine, you walk away and don't touch the terminal. If it drifts, you have a fast steering wheel and never have to switch out of the terminal to edit files.

### Permission handling

Three modes, controlled by `config.yaml` via `permissions.mode`:

**Safe mode** (`mode: safe`, default):
- `--permission-mode acceptEdits` — auto-accepts file edits (`Edit`, `Write`, `MultiEdit`, `NotebookEdit`).
- `--allowedTools` with a curated list covering tool names + Bash glob patterns: `git status`, `git commit *`, `uv run *`, `pytest *`, `ruff *`, etc. (See `simpleharness_core.DEFAULT_BASH_ALLOW`.)
- Bash commands outside the allowlist **fail cleanly** — the agent sees the failure, reports it in its phase file, and the harness loop continues (or blocks per loop guards).
- Per-worksite extension: add patterns to `permissions.extra_bash_allow` in `<worksite>/simpleharness/config.yaml`.
- Per-role extension: add `allowed_tools: [...]` in the role's frontmatter.

**Approver mode** (`mode: approver`):
- Same `acceptEdits` + `--allowedTools` floor as safe mode, PLUS a PreToolUse hook registered via `--settings` that runs on every Bash call.
- The hook is a two-layer design. A bash fast path (`simpleharness_approver_hook.sh`) parses the tool-use envelope via `jq`, matches the command against a per-task allowlist file (`<task>/.approver-allowlist.txt`) using `case` glob matching, and emits an allow verdict in ~30-60ms on hit. Miss → `exec`s a Python slow path.
- The Python slow path (`simpleharness_approver_hook.py`) spawns a dedicated approver session — a fresh `claude -p` running the `approver` role on `sonnet` (configurable via `permissions.approver_model`) with `--append-system-prompt-file roles/approver.md`. The approver sees the tool call, the working agent's last ~30 lines of reasoning, and the list of already-approved patterns, then returns a structured JSON verdict: allow (with a glob pattern covering the full safe usage surface) or deny (with a one-sentence reason). Destructive operations (`rm -rf`, `curl | sh`, force pushes, sudo, writes outside the worksite) are hard-denied by the role body and never land in the allowlist.
- On allow, the slow path persists the pattern to `permissions.extra_bash_allow` in the worksite `config.yaml` AND refreshes `.approver-allowlist.txt` so future calls in the same session hit the bash fast path. Cross-process races are prevented by a shared file lock (`simpleharness_core.persist_approver_allow`).
- Approver sessions are logged to `logs/<task>/approver-<timestamp>.jsonl` alongside the normal phase logs, so you can audit every allow/deny after the fact.
- Optional: `permissions.escalate_denials_to_correction: true` appends denial blocks to the task's `CORRECTION.md` so you can override them on resume.
- The approver grows each worksite's `extra_bash_allow` list over time. You can periodically prune or promote patterns by hand-editing the worksite config.

**Dangerous mode** (`mode: dangerous`):
- Switches to `--permission-mode bypassPermissions`, which approves everything.
- **Only safe in a sandboxed dev container or VM.**
- `simpleharness doctor` refuses to start `watch` in dangerous mode unless it detects a sandbox marker (`/.dockerenv`, `SIMPLEHARNESS_SANDBOX=1`) or you pass `--i-know-its-dangerous`.

The safe mode floor is the important guarantee: no matter how much an agent wants to run `curl example.com | sh`, it can't, and the failure is observable. Approver mode extends that floor with a dedicated reviewer that judges unknown commands on their merits — hands-off operation with defense-in-depth beyond a static allowlist.

### Self-improvement

The **project-leader** role is privileged: it's the only role allowed to edit files in the toolbox repo — other role files, workflow files, `config.yaml`, and its own role file. This means SimpleHarness can iterate on its own prompts and orchestration over time, based on what works in real tasks. When project-leader edits the toolbox, it commits the change with a clear message.

The user enforces concurrency safety by only instructing one running instance at a time to do meta-work; the harness does not lock the toolbox programmatically. (If this becomes a real problem, a lock file is the obvious next step.)

## How a task flows end-to-end

1. **You** write a `TASK.md` (or run `simpleharness new "<title>" --workflow=<name>`) in your worksite.
2. **You** run `simpleharness watch` from the worksite (or `simpleharness watch --worksite PATH` from elsewhere).
3. **The harness** scans for active tasks, picks one, decides the next role, builds the session prompt (preamble + correction if any + task brief), and spawns `claude -p` with the right system prompt + cwd + permissions.
4. **The agent** reads `TASK.md` and any prior phase files, dispatches subagents for mechanical work, produces its phase file, updates `STATE.md` narrowly (only `status`, `phase`, `next_role`, `blocked_reason`), commits worksite code if applicable, and exits.
5. **The harness** reads the updated `STATE.md`, applies bookkeeping (session count, no-progress detection, loop guards), and either advances to the next role, blocks the task, or marks it done.
6. **You** are still walking your dog or working in another window. If the agent drifts, you `Ctrl+C` → type → resume.
7. Eventually project-leader writes `FINAL.md` with the summary, sets `status=done`, and the harness moves on to the next active task or goes idle.

## Project structure

### Toolbox (this repo)

```
SimpleHarness/
├── intent.md                      # this file — the canonical project doc
├── README.md                      # install + quick start + commands
├── pyproject.toml                 # uv project, declares the simpleharness CLI entry point
├── harness.py                     # the entire tool, single ~800-line file
├── config.yaml                    # toolbox defaults: model, permissions, loop cadence, caps
├── .gitattributes                 # LF normalization for cross-platform sanity
├── .claude/settings.json          # mirrors the safe-mode allowlist for interactive toolbox debug
├── roles/
│   ├── project-leader.md          # privileged orchestrator
│   ├── brainstormer.md
│   ├── plan-writer.md
│   ├── developer.md
│   └── expert-critic.md
└── workflows/
    ├── universal.md               # phases: [project-leader]
    └── feature-build.md           # 6-phase structured chain
```

### Per-worksite (created by `simpleharness init`)

```
<your-worksite>/
├── (your project files, git repo)
└── simpleharness/
    ├── config.yaml                # optional per-worksite overrides (rare)
    ├── tasks/
    │   └── 001-some-slug/
    │       ├── TASK.md            # you write this — the brief
    │       ├── STATE.md           # harness pointer (yaml frontmatter only)
    │       ├── 00-kickoff.md      # phase files, one per session
    │       ├── 01-brainstorm.md
    │       ├── 02-plan.md
    │       ├── 03-develop.md
    │       ├── 04-critique.md
    │       ├── FINAL.md           # written by project-leader at the end
    │       ├── CORRECTION.md      # transient — present only between Ctrl+C and next session
    │       └── .session_prompt.md # transient — rebuilt before every session
    ├── memory/WORKSITE.md         # long-term notes any session may append to
    └── logs/
        └── 001-some-slug/
            ├── 00-project-leader.jsonl    # full stream-json dump per session
            └── 00-project-leader.log      # plain-text mirror
```

`STATE.md` is the harness-owned pointer file. Agents may only edit four fields (`status`, `phase`, `next_role`, `blocked_reason`) and must use `Edit` not `Write` to preserve everything else.

## CLI surface

```
simpleharness --version
simpleharness init     [--worksite PATH]                       create simpleharness/ in a worksite
simpleharness new      "<title>" [--workflow=<name>] [--worksite PATH]
simpleharness watch    [--once] [--worksite PATH]              long-lived loop (or single tick)
simpleharness status   [--worksite PATH]                       list active tasks + phase
simpleharness list     [--worksite PATH]                       list all tasks
simpleharness show     <slug> [--worksite PATH]                details of one task
simpleharness doctor   [--worksite PATH]                       sanity checks
```

`--worksite` defaults to the current directory. The `SIMPLEHARNESS_WORKSITE` env var is another override. This is mostly for CI scripts and self-tests; normal usage is just `cd` into your worksite and run.

## What SimpleHarness deliberately is NOT

- **Not a full agent framework.** No DAGs, no parallel sessions, no message-passing fabric, no SDK. One Python file, sequential sessions.
- **Not a Claude Code replacement.** It wraps the existing CLI as a subprocess. If you want interactive Claude, just run `claude`.
- **Not opinionated about your domain.** Workflows are pluggable markdown. Roles are pluggable markdown. The Python code knows nothing about coding vs writing vs research — that lives entirely in your role library.
- **Not a daemon or service.** It's a long-lived foreground process that you `Ctrl+C` when you want it to stop.
- **Not fault-tolerant against malicious roles.** The toolbox is editable by privileged roles (project-leader). The user is responsible for reviewing what self-improvement edits were made before running the next task.

## Status

**v0.1 MVP.** Single-tick `watch --once` is verified end-to-end against a real `claude` invocation: install, doctor, init, new, scaffold, role/workflow load, prompt build, subprocess spawn, stream rendering, STATE round-trip. The full long-lived `watch` loop is implemented but not yet stress-tested. The five seed roles are short first drafts (~50 lines of body each) and will iterate based on real-task feedback. The intervention flow (`Ctrl+C` → stdin → `CORRECTION.md`) is implemented but pending live verification with a real session.

The spec for everything in this file lives in `~/.claude/plans/recursive-mapping-squid.md` (the design plan from the brainstorming pass that produced this codebase).
