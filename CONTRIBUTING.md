# Contributing to SimpleHarness

## How SimpleHarness is meant to be used

SimpleHarness is a "clone and customize" tool. Most users fork the repo, write their own roles and workflows, and run it against their own projects. Contributions back to the main repo are welcome, especially improvements to the core harness, new seed roles, bug fixes, and documentation.

## Getting set up

```bash
git clone https://github.com/OleJBondahl/SimpleHarness.git
cd SimpleHarness
uv sync
uvx pre-commit install
```

You need Python 3.13+ and [uv](https://docs.astral.sh/uv/) installed.

## Running the checks

Run each command separately (do not chain with `&&`):

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check
```

All four must pass before you submit a PR.

## Code style

- **Functional core, imperative shell.** Pure logic goes in `core.py` or `approver_core.py`. I/O, subprocesses, and filesystem access go in `shell.py` or `approver_shell.py`.
- **Every function in a core module must be decorated with `@deal.pure`.** The pre-commit hook enforces this. If you skip the decorator, CI will catch it.
- **Frozen dataclasses** in core modules. Rebuild with `dataclasses.replace()` instead of mutating.
- **Return results, don't raise exceptions** in core. Use `Ok`/`Err` return types for expected failures.

See [docs/intent.md](docs/intent.md) for the reasoning behind these choices.

## What to contribute

Good first contributions:
- Bug fixes with a test that reproduces the issue
- New seed roles (add to `src/simpleharness/roles/`)
- New seed workflows (add to `src/simpleharness/workflows/`)
- Documentation improvements
- Better error messages

Before starting large changes, open an issue to discuss the approach.

## Pull request checklist

- [ ] Tests pass (`uv run pytest`)
- [ ] No lint warnings (`uv run ruff check .`)
- [ ] Types check (`uv run ty check`)
- [ ] New core functions have `@deal.pure`
- [ ] Commit messages explain the "why"

## Reporting bugs

Open a [GitHub issue](https://github.com/OleJBondahl/SimpleHarness/issues) with:
- What you expected to happen
- What actually happened
- Steps to reproduce
- Your OS, Python version, and Claude Code version

## Security issues

Do not open public issues for security vulnerabilities. See [SECURITY.md](SECURITY.md) for the reporting process.
