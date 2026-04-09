"""Pure scoring logic for benchmark runs.

Contains ONLY pure code: frozen dataclasses and @deal.pure functions.
No file I/O, subprocess calls, or environment reads.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import deal

# ────────────────────────────────────────────────────────────────────────────
# Dataclasses
# ────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TaskResult:
    """Result of a single benchmark task."""

    name: str
    task_type: str  # bugfix, feature, refactor, research, cli
    language: str
    tests_passed: bool
    tests_regression: bool
    cost_usd: float
    total_sessions: int
    no_progress_ticks: int
    deliverables_present: bool
    duration_seconds: float
    score: int  # computed by compute_task_score


@dataclass(frozen=True)
class AggregateResult:
    """Aggregate statistics across all tasks in a benchmark run."""

    total_score: int
    max_possible: int
    total_cost_usd: float
    pass_rate: float  # fraction of tasks where tests_passed


@dataclass(frozen=True)
class BenchmarkRun:
    """A complete benchmark run with task results and aggregate."""

    run_id: str  # timestamp-based ID
    harness_commit: str
    tasks: tuple[TaskResult, ...] = field(default_factory=tuple)
    aggregate: AggregateResult = field(
        default_factory=lambda: AggregateResult(
            total_score=0, max_possible=0, total_cost_usd=0.0, pass_rate=0.0
        )
    )


# ────────────────────────────────────────────────────────────────────────────
# Pure functions
# ────────────────────────────────────────────────────────────────────────────


@deal.pure
def compute_efficiency(total_sessions: int, no_progress_ticks: int) -> int:
    """Compute efficiency score (0-100) from session count and stall ticks."""
    return max(0, 100 - (total_sessions * 15) - (no_progress_ticks * 25))


@deal.pure
def compute_task_score(
    tests_passed: bool,
    tests_regression: bool,
    total_sessions: int,
    no_progress_ticks: int,
    deliverables_present: bool,
) -> int:
    """Compute overall task score (0-100).

    Breakdown:
      - tests_passed:        40 points
      - no regression:       20 points
      - efficiency:          20 points (scaled from compute_efficiency)
      - deliverables_present: 20 points
    """
    efficiency = compute_efficiency(total_sessions, no_progress_ticks)
    return (
        int(tests_passed) * 40
        + int(not tests_regression) * 20
        + (efficiency * 20 // 100)
        + int(deliverables_present) * 20
    )


@deal.pure
def aggregate_results(task_results: tuple[TaskResult, ...]) -> AggregateResult:
    """Aggregate scores, costs, and pass rate across task results."""
    if not task_results:
        return AggregateResult(total_score=0, max_possible=0, total_cost_usd=0.0, pass_rate=0.0)
    total_score = 0
    total_cost = 0.0
    passed_count = 0
    for t in task_results:
        total_score += t.score
        total_cost += t.cost_usd
        if t.tests_passed:
            passed_count += 1
    return AggregateResult(
        total_score=total_score,
        max_possible=len(task_results) * 100,
        total_cost_usd=total_cost,
        pass_rate=passed_count / len(task_results),
    )


@deal.pure
def build_benchmark_run(
    run_id: str,
    harness_commit: str,
    task_results: tuple[TaskResult, ...],
) -> BenchmarkRun:
    """Build a complete BenchmarkRun from task results."""
    return BenchmarkRun(
        run_id=run_id,
        harness_commit=harness_commit,
        tasks=task_results,
        aggregate=aggregate_results(task_results),
    )
