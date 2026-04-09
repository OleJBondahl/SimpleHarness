# Benchmark Loop

A self-improving feedback loop for optimizing SimpleHarness roles, config, and workflows.

## How it works

The benchmark system runs small, self-contained tasks through the harness, scores the results objectively, and feeds findings to an analyst role that proposes improvements.

```
Snapshot Repos ──> benchmark run ──> Score Report ──> benchmark analyze
     ^                                                      |
     |                                                      v
     └──────────────── You approve <──────────── PROPOSAL.md
```

**One optimization cycle:**

1. `simpleharness benchmark run` executes all benchmark tasks, runs their test suites, and writes scored results to `results/`
2. `simpleharness benchmark analyze` spawns the analyst role to review results + session traces and write `PROPOSAL.md`
3. You review the proposal, apply approved changes to roles/config
4. Re-run to verify improvement

## Quick start

```bash
# Run all benchmark tasks and see scores
simpleharness benchmark run

# Run from a custom directory
simpleharness benchmark run --benchmarks-dir path/to/benchmarks

# Analyze results (spawns analyst role)
simpleharness benchmark analyze
```

## Scoring

Each task is scored 0-100 from four objective metrics:

| Component | Points | What it measures |
|---|---|---|
| Tests pass | 40 | Did the agent fix the bug / implement the feature? |
| No regressions | 20 | Did previously-passing tests stay passing? |
| Efficiency | 20 | Fewer sessions and no stalls = higher score |
| Deliverables | 20 | Are all expected output files present? |

Efficiency is computed as `max(0, 100 - (sessions * 15) - (stalls * 25))`, then scaled to 20 points. A task solved in 1 session with no stalls gets full marks.

Results are written to `results/<timestamp>.json` for historical comparison.

## Snapshot repos

Each benchmark task is a tiny, self-contained git repo in `repos/` with:

| File | Purpose |
|---|---|
| `benchmark-meta.yaml` | Test command, task type, language, cost cap |
| `TASK.md` | SimpleHarness task spec (goal, success criteria, boundaries) |
| Source + tests | A seeded problem with failing tests |

Repos are reset to their `benchmark-start` git tag before each run, so results are reproducible.

### Current tasks

| Repo | Type | Language | Problem |
|---|---|---|---|
| `bugfix-retry-logic` | Bugfix | Python | Off-by-one error in HTTP retry logic (3 pass, 1 fail) |
| `feature-cli-json` | Feature | Python | Missing `--format json` flag on a CLI tool (2 pass, 1 fail) |

### Adding a new benchmark task

1. Create a directory under `repos/` with source code, tests, and a seeded problem
2. Add `benchmark-meta.yaml`:
   ```yaml
   test_command: "uv run pytest"
   expected_tests_pass: true
   max_cost_usd: 5.0
   task_type: bugfix    # bugfix | feature | refactor | research | cli
   language: python
   ```
3. Add `TASK.md` in SimpleHarness format (frontmatter with title, workflow, deliverables + markdown body with goal and success criteria)
4. Initialize git and tag the starting state:
   ```bash
   cd repos/my-new-task
   git init
   git add .
   git commit -m "initial: my-new-task benchmark"
   git tag benchmark-start
   ```
5. Verify: run `uv run pytest` inside the repo and confirm the expected tests fail

## Directory layout

```
benchmarks/
  README.md           # this file
  PROPOSAL.md         # analyst's latest improvement proposal (generated)
  repos/              # snapshot repos (one per benchmark task)
    bugfix-retry-logic/
    feature-cli-json/
  results/            # scored JSON results (one per run, kept for history)
    2026-04-09T13-07-36.json
  traces/             # JSONL session logs per run (for analyst review)
```

## Architecture

The scoring logic is split into two modules following the functional-core / imperative-shell pattern:

- **`benchmark_core.py`** (pure) — frozen dataclasses (`TaskResult`, `AggregateResult`, `BenchmarkRun`) and `@deal.pure` scoring functions
- **`benchmark.py`** (shell) — git resets, subprocess test runs, file I/O, result serialization

The **`benchmark-analyst`** role (`roles/benchmark-analyst.md`) reads scores and traces, maps failures to optimization levers (correctness, context precision, workflow efficiency, regression safety), and writes targeted proposals with diffs.

## Current limitations

- `benchmark run` currently runs tests directly against snapshot repos without spawning full harness sessions. Full harness integration (with `tick_once`) is planned.
- `benchmark analyze` is a stub pending session integration.
- Only 2 benchmark tasks exist. 3-5 across different languages and task types is recommended to prevent overfitting.
