# SimpleHarness

A Python harness for running and supervising Claude Code sessions (approver mode, MCP servers, role prompts, dev containers).

## Strongly Recommended Skill

**Use the `python-coding-and-tooling` skill for all Python work in this repo.** It defines the mandatory toolchain (`uv`, `ruff`, `ty`, `pytest`, `deal`), the functional-core / imperative-shell layout, the `pyproject.toml` template, and the rules for core modules (frozen dataclasses, Result-shaped returns, `@deal.pure`). Load it before writing or editing Python code here.

Invoke via the Skill tool: `python-coding-and-tooling`.

## Project-Specific Notes

- **Python 3.13** via `uv`. System Python path: `/c/Users/OleJohanBondahl/AppData/Local/Programs/Python/Python313/python.exe`
- `ruff` (100-col, `extend-select = ["I", "B", "UP", "SIM", "RUF"]`) and `ty` are wired into a PostToolUse hook that runs on every `.py` edit.
- Heavier quality checks (`complexipy`, `bandit`, `vulture`, `radon`) are not in the fast hook — run them explicitly when needed.
- The FC/IS refactor is **in progress**, not complete. Existing code predates the split; don't refactor legacy modules to FC/IS unless asked. New modules (and new functions in existing modules when scope allows) should follow the style in the skill.
- The detailed FC/IS migration plan lives in `~/.claude/plans/elegant-marinating-cocke.md`.

## Common Commands

```bash
uv sync                              # install + lock
uv run pytest                        # run tests
uv run ruff check .                  # lint
uv run ruff format .                 # format
uv run ty check                      # type check
```

Never chain with `&&` / `;` — a hook blocks it. Run each command separately.

## Architecture (high level)

- **`src/simpleharness/`** — core harness logic, role loaders, MCP server wrappers.
- **`src/simpleharness_approver_mcp/`** — local MCP server for the approver role.
- **`roles/`** — role-specific prompts and configuration.
- **`tests/`** — pytest suite.
- **`claude-tools/`** — ad-hoc scripts (gitignored).

See `pyproject.toml` for the authoritative dependency and tool configuration.
