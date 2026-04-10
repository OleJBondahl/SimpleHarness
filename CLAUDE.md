# SimpleHarness

A Python harness for running and supervising Claude Code sessions (approver hook, role prompts, dev containers).

## Strongly Recommended Skill

**Use the `python-coding-and-tooling` skill for all Python work in this repo.** It defines the mandatory toolchain (`uv`, `ruff`, `ty`, `pytest`, `deal`), the functional-core / imperative-shell layout, the `pyproject.toml` template, and the rules for core modules (frozen dataclasses, Result-shaped returns, `@deal.pure`). Load it before writing or editing Python code here.

Invoke via the Skill tool: `python-coding-and-tooling`.

## Codebase Orientation

Before exploring source files, orient yourself through the auto-generated context in `.codesight/`. This map is regenerated on every commit via pre-commit hook.

1. **Start here:** Read `.codesight/wiki/index.md` (~200 tokens) for a subsystem overview and article list.
2. **For function signatures:** Read `.codesight/libs.md` — complete index of exported functions with parameters across all modules.
3. **For the full picture:** Read `.codesight/CODESIGHT.md` (~4000 tokens) — combined libs, config, and coverage map.

Only after identifying the relevant files through the map should you read actual source files. Do not glob or grep the codebase for orientation — the map already has it.

## Project-Specific Notes

- **Python 3.13** via `uv`.
- `ruff` (100-col, 18 rule sets including S, C90, D, PERF, FURB, N, TCH) and `ty` (all rules = error) are wired into a PostToolUse hook that runs on every `.py` edit.
- Quality gates in pre-commit: ruff, ty, deal-lint, fp-purity-gate, import-linter (architecture boundaries), vulture (dead code), complexipy (cognitive complexity), codesight, detect-secrets.
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
| `src/simpleharness/simpleharness_approver_hook.sh` | Bash fast path for the approver hook |
| `scripts/check_fp_purity.py` | AST gate: every function in core must be `@deal.pure` decorated |
| `tests/test_core.py` + `tests/test_approver_core.py` | pytest suite (~125 tests, ~99% coverage on core modules) |

**Pre-commit gates:**
- `deal-lint` — detects impurity violations inside `@deal.pure`-decorated functions
- `fp-purity-gate` — enforces that every function in `core.py` / `approver_core.py` is decorated

**Other dirs:**
- **`src/simpleharness/roles/`** — role-specific prompts and configuration.
- **`tests/`** — pytest suite.
- **`claude-tools/`** — ad-hoc scripts (gitignored).

See `pyproject.toml` for the authoritative dependency and tool configuration.

## For Contributors

### Setup from scratch

```bash
git clone https://github.com/OleJBondahl/SimpleHarness.git
cd SimpleHarness
uv sync                        # install dependencies
uvx pre-commit install         # enable ruff + ty pre-commit hook
```

### Development commands

```bash
uv run pytest                  # run tests (~125 tests, ~99% core coverage)
uv run ruff check .            # lint
uv run ruff format .           # format
uv run ty check                # type check
```

### Code quality gates

Every function in `core.py` and `approver_core.py` must be decorated with `@deal.pure`. Two pre-commit hooks enforce this:

- **`deal-lint`** — detects impurity violations inside `@deal.pure` functions
- **`fp-purity-gate`** — enforces that every function has the decorator

See [docs/usage.md](docs/usage.md) for detailed usage reference, TASK.md schema, and directory layout.
