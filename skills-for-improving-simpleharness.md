# Skills for improving SimpleHarness

Skills used when **we edit SimpleHarness itself** — adding features, fixing bugs, writing instructions, hardening the package, shipping releases.

Priority legend: **Core** = use by default · **Useful** = pull in when the task touches this area · **Optional** = polish / later.

---

## A. Claude Code surface & plugin authoring

Relevant when working on `harness.py`, `simpleharness_core.py`, `simpleharness_approver_mcp.py`, hooks, or anything that touches the Claude Code CLI surface.

| Skill | What it does | When to use | Priority |
|---|---|---|---|
| `claude-api` | Anthropic SDK + Agent SDK reference — current model IDs, adaptive thinking, streaming, prompt caching, pitfalls | Any code that calls the Anthropic SDK or Agent SDK from inside the harness | Core (if SDK used) |
| `plugin-dev/hook-development` | Writing `PreToolUse` / `PostToolUse` / `SessionStart` / `UserPromptSubmit` hooks | Approver mode, permission gates, auto-checks — the heart of SimpleHarness | Core |
| `plugin-dev/agent-development` | Subagent definition format and conventions | Editing or adding files in `roles/` | Core |
| `plugin-dev/plugin-structure` | Layout of a Claude Code plugin | If SimpleHarness is shipped as a plugin | Useful |
| `plugin-dev/plugin-settings` | `settings.json` schema and validation | When exposing harness config via settings | Useful |
| `plugin-dev/command-development` | Slash command authoring | If SimpleHarness exposes slash commands | Optional |
| `plugin-dev/mcp-integration` | Wiring MCP servers into a plugin | Changes to `simpleharness_approver_mcp.py` integration | Useful |
| `mcp-server-dev/build-mcp-server` | Building a real MCP server from scratch | Extending the approver MCP server | Useful |
| `skill-creator` / `writing-skills` | Authoring new skills | If SimpleHarness ships bundled skills | Useful |
| `update-config` | Configuring Claude Code via `settings.json` + hooks | Any change to harness config surface | Core |
| `hookify/writing-rules` | Declarative hookify rules (pattern → hook) | Design reference for approver rule logic | Useful |

---

## B. Instructions & repo hygiene

Relevant when editing `CLAUDE.md`, `intent.md`, `dev-container.md`, `README.md`, or the MCP memory store.

| Skill | What it does | When to use | Priority |
|---|---|---|---|
| `claude-md-improver` | Audits and improves `CLAUDE.md` files against a quality template | Run regularly on SimpleHarness's own CLAUDE.md | Core |
| `claude-automation-recommender` | Recommends hooks / commands / skills for a repo | Discovery pass to find automation gaps | Useful |
| `updating-memory` | Keeps MCP memory server fresh after code/API changes | After any task that changes functions, classes, config | Core |

---

## C. Python package development (`python-skills/*`)

SimpleHarness is a Python CLI package (`pyproject.toml`, `uv`, `ruff`, `ty`). These are the library-dev skills that map directly to it.

| Skill | What it does | When to use | Priority |
|---|---|---|---|
| `python-skills/cli-development` | Click / Typer / argparse patterns | `simpleharness watch` and any CLI surface work | Core |
| `python-skills/packaging` | `pyproject.toml`, `uv`, entry points, wheels | Package metadata and distribution | Core |
| `python-skills/code-quality` | `ruff` + `ty` configuration (already in use) | When linter/type-checker config needs updating | Core |
| `python-skills/testing-strategy` | pytest, fixtures, Hypothesis | Writing or refactoring tests | Core |
| `python-skills/security-audit` | bandit, dependency scanning | Harness handles permissions — audit regularly | Core |
| `python-skills/api-design` | Public API surface, versioning | When changing `simpleharness_core` public functions | Useful |
| `python-skills/project-setup` | `PYPROJECT.md`, `CI.md`, `MAKEFILE.md` references | New project scaffolding or CI changes | Useful |
| `python-skills/release-management` | Versioning, changelogs, PyPI releases | Cutting a release | Useful |
| `python-skills/performance` | Profiling, optimization | Only if harness hits a perf bottleneck | Optional |
| `python-skills/documentation` | Docstring / Sphinx / MkDocs patterns | When building out user docs | Optional |
| `python-skills/community` | Contributor guides, governance | If SimpleHarness grows external contributors | Optional |
| `python-skills/library-review` | End-to-end library quality review | Periodic health check | Optional |

---

## D. Workflow discipline we apply to our own edits

These shape **how** we work on SimpleHarness, regardless of what's being changed.

| Skill | What it does | When to use | Priority |
|---|---|---|---|
| `brainstorming` | Explores intent, requirements, design before implementation | Before any creative/feature work | Core |
| `writing-plans` | Turn a spec into an executable plan file | Any multi-step task | Core |
| `executing-plans` | Run a plan with review checkpoints | After a plan is written | Core |
| `test-driven-development` | Test-first discipline | Any feature or bugfix with observable behavior | Core |
| `verification-before-completion` | Requires evidence (commands run, output shown) before "done" | Before committing or claiming success | Core |
| `systematic-debugging` | Structured root-cause analysis | Any bug, failing test, or unexpected behavior | Core |
| `requesting-code-review` | Self-review checklist before merge | Before opening PRs | Useful |
| `receiving-code-review` | How to respond to review feedback rigorously | When addressing PR comments | Useful |
| `finishing-a-development-branch` | Structured merge / PR / cleanup | At the end of a feature branch | Useful |
| `using-git-worktrees` | Worktree isolation for parallel work | When two changes can proceed independently | Useful |
| `dispatching-parallel-agents` | 2+ independent tasks → parallel subagents | Multi-area refactors | Useful |
| `haiku-delegate` | Push mechanical work (search, read, exec) to Haiku | Mandated by global CLAUDE.md — always | Core |
| `visual-review` | Render-and-read loop for diagrams / PDFs | If SimpleHarness generates visual output | Optional |
