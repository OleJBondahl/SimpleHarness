# Changelog

All notable changes to SimpleHarness are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.0-alpha] - 2026-04-10

First public alpha. Clone it, break it, tell us what's missing.

### Added

- Baton-pass loop: sequences of short `claude -p` sessions, each with a fresh context window and a single role
- Role system: markdown-based role prompts (project-leader, brainstormer, plan-writer, developer) you can swap or replace
- Workflow engine: define phase sequences in markdown. Ships with `universal` and `feature-build`
- Three permission modes: `safe` (curated allowlist), `approver` (two-layer hook with Sonnet fallback), `dangerous` (dev containers only)
- Cost tracking: per-session and per-task USD cost and duration
- Task management: `new`, `list`, `show`, `status` commands with dependency tracking and deliverable verification
- Mid-flight steering: Ctrl+C kills the current session and drops you into a correction prompt
- Rich dashboard: `simpleharness status` shows progress, cost, and blocked reasons
- Dev container support: unattended runs with sandbox detection via `simpleharness doctor`
- Functional core / imperative shell architecture: all harness logic is pure (`@deal.pure` enforced), all I/O lives in shell modules
- ~125 tests at ~99% coverage on core modules

### Known limitations

- No PyPI distribution yet. Clone the repo and run `uv sync`
- Workflow definitions are rigid sequences (no conditional branching)
- Approver hook cache has no TTL or size limit
- Cost tracking depends on Claude Code CLI output format, may break on CLI updates

[0.1.0-alpha]: https://github.com/OleJBondahl/SimpleHarness/releases/tag/v0.1.0-alpha
