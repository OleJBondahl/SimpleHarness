# SimpleHarness

A Python harness for running and supervising Claude Code sessions (approver hook, role prompts, dev containers).

## Strongly Recommended Skill

**Use the `python-coding-and-tooling` skill for all Python work in this repo.** It defines the mandatory toolchain (`uv`, `ruff`, `ty`, `pytest`, `deal`), the functional-core / imperative-shell layout, the `pyproject.toml` template, and the rules for core modules (frozen dataclasses, Result-shaped returns, `@deal.pure`). Load it before writing or editing Python code here.

Invoke via the Skill tool: `python-coding-and-tooling`.

## Project-Specific Notes

- **Python 3.13** via `uv`. System Python path: `/c/Users/OleJohanBondahl/AppData/Local/Programs/Python/Python313/python.exe`
- `ruff` (100-col, `extend-select = ["I", "B", "UP", "SIM", "RUF"]`) and `ty` are wired into a PostToolUse hook that runs on every `.py` edit.
- Heavier quality checks (`complexipy`, `bandit`, `vulture`, `radon`) are not in the fast hook — run them explicitly when needed.
- The FC/IS refactor is **complete**. All logic is split into pure core modules and impure shell modules.

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

FC/IS split: pure core modules (frozen dataclasses, `@deal.pure` on every function) + impure shell modules (I/O, subprocess, CLI).

| File | Role |
|---|---|
| `src/simpleharness/core.py` | Pure harness logic — FP-enforced, `@deal.pure` decorated |
| `src/simpleharness/approver_core.py` | Pure approver decision logic — FP-enforced |
| `src/simpleharness/shell.py` | CLI entry point, file I/O, subprocess, tick loop |
| `src/simpleharness/approver_shell.py` | PreToolUse hook slow path (impure orchestration) |
| `simpleharness_approver_hook.sh` | Bash fast path for the approver hook |
| `scripts/check_fp_purity.py` | AST gate: every function in core must be `@deal.pure` decorated |
| `tests/test_core.py` + `tests/test_approver_core.py` | pytest suite (~125 tests, ~99% coverage on core modules) |

**Pre-commit gates:**
- `deal-lint` — detects impurity violations inside `@deal.pure`-decorated functions
- `fp-purity-gate` — enforces that every function in `core.py` / `approver_core.py` is decorated

**Other dirs:**
- **`roles/`** — role-specific prompts and configuration.
- **`tests/`** — pytest suite.
- **`claude-tools/`** — ad-hoc scripts (gitignored).

See `pyproject.toml` for the authoritative dependency and tool configuration.
