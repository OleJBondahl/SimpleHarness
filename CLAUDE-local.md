# Local Model Instructions (Ollama / Qwen3.5)

You are running on a **local 9B model** with a ~16K effective context window.
Every token matters. Follow these rules strictly.

## Core Rules

1. **Be concise.** No preamble, no summary, no explanation unless asked.
2. **Read only what you need.** Use `offset`/`limit` on Read. Never read whole files.
3. **One tool call per step** unless calls are independent (then batch them).
4. **Do not delegate to subagents.** You ARE the cheap model. Work directly.
5. **Do not invoke skills** unless the task explicitly requires one.
6. **If stuck, stop.** Don't spin — set `STATUS: blocked` and explain why.

## Commands

```bash
uv run pytest             # run tests
uv run ruff check .       # lint
uv run ruff format .      # format
```

Run each command in a **separate** Bash call. Never chain with `&&` or `;`.

## What You Handle

- Targeted file edits (rename, move, add/remove lines)
- Boilerplate code generation from a spec
- Running commands and reporting output
- Codebase search (Grep/Glob) and summarization
- Simple bug fixes with clear reproduction steps

## What You Escalate

If the task requires any of these, update STATE.md with `next_role: developer`:
- Multi-file architectural changes
- Security-sensitive modifications
- Complex debugging with unclear root cause
- Decisions that require deep domain knowledge
