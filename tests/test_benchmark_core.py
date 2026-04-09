"""Tests for pure functions in simpleharness.benchmark_core."""

from __future__ import annotations

import pytest

from simpleharness.benchmark_core import (
    AggregateResult,
    TaskResult,
    aggregate_results,
    build_benchmark_run,
    compute_efficiency,
    compute_task_score,
)

# ────────────────────────────────────────────────────────────────────────────
# Factory helpers
# ────────────────────────────────────────────────────────────────────────────


def _task_result(
    *,
    name: str = "task-001",
    task_type: str = "bugfix",
    language: str = "python",
    tests_passed: bool = True,
    tests_regression: bool = False,
    cost_usd: float = 0.50,
    total_sessions: int = 1,
    no_progress_ticks: int = 0,
    deliverables_present: bool = True,
    duration_seconds: float = 120.0,
    score: int | None = None,
) -> TaskResult:
    if score is None:
        score = compute_task_score(
            tests_passed, tests_regression, total_sessions, no_progress_ticks, deliverables_present
        )
    return TaskResult(
        name=name,
        task_type=task_type,
        language=language,
        tests_passed=tests_passed,
        tests_regression=tests_regression,
        cost_usd=cost_usd,
        total_sessions=total_sessions,
        no_progress_ticks=no_progress_ticks,
        deliverables_present=deliverables_present,
        duration_seconds=duration_seconds,
        score=score,
    )


# ────────────────────────────────────────────────────────────────────────────
# compute_efficiency
# ────────────────────────────────────────────────────────────────────────────


class TestComputeEfficiency:
    def test_zero_sessions_zero_stalls(self) -> None:
        assert compute_efficiency(0, 0) == 100

    def test_one_session_no_stalls(self) -> None:
        assert compute_efficiency(1, 0) == 85

    def test_two_sessions_no_stalls(self) -> None:
        assert compute_efficiency(2, 0) == 70

    def test_one_session_one_stall(self) -> None:
        assert compute_efficiency(1, 1) == 60

    def test_many_sessions_clamps_to_zero(self) -> None:
        assert compute_efficiency(10, 5) == 0

    def test_only_stalls(self) -> None:
        assert compute_efficiency(0, 4) == 0

    def test_moderate_usage(self) -> None:
        # 3 sessions, 1 stall: 100 - 45 - 25 = 30
        assert compute_efficiency(3, 1) == 30


# ────────────────────────────────────────────────────────────────────────────
# compute_task_score
# ────────────────────────────────────────────────────────────────────────────


class TestComputeTaskScore:
    def test_perfect_score(self) -> None:
        # tests_passed=40, no_regression=20, efficiency=100→20, deliverables=20
        score = compute_task_score(
            tests_passed=True,
            tests_regression=False,
            total_sessions=0,
            no_progress_ticks=0,
            deliverables_present=True,
        )
        assert score == 100

    def test_all_fail(self) -> None:
        # tests_passed=0, regression=0, efficiency clamped→0, deliverables=0
        score = compute_task_score(
            tests_passed=False,
            tests_regression=True,
            total_sessions=10,
            no_progress_ticks=10,
            deliverables_present=False,
        )
        assert score == 0

    def test_tests_passed_but_regression(self) -> None:
        # 40 + 0 + 20*(85/100) + 20 = 40 + 0 + 17 + 20 = 77
        score = compute_task_score(
            tests_passed=True,
            tests_regression=True,
            total_sessions=1,
            no_progress_ticks=0,
            deliverables_present=True,
        )
        assert score == 77

    def test_no_deliverables(self) -> None:
        # 40 + 20 + 20*(85/100) + 0 = 40 + 20 + 17 + 0 = 77
        score = compute_task_score(
            tests_passed=True,
            tests_regression=False,
            total_sessions=1,
            no_progress_ticks=0,
            deliverables_present=False,
        )
        assert score == 77

    def test_multiple_sessions_reduce_efficiency(self) -> None:
        # 40 + 20 + 20*(30/100) + 20 = 40 + 20 + 6 + 20 = 86
        score = compute_task_score(
            tests_passed=True,
            tests_regression=False,
            total_sessions=3,
            no_progress_ticks=1,
            deliverables_present=True,
        )
        assert score == 86

    def test_only_no_regression_points(self) -> None:
        # 0 + 20 + 0 + 0 = 20
        score = compute_task_score(
            tests_passed=False,
            tests_regression=False,
            total_sessions=10,
            no_progress_ticks=10,
            deliverables_present=False,
        )
        assert score == 20


# ────────────────────────────────────────────────────────────────────────────
# aggregate_results
# ────────────────────────────────────────────────────────────────────────────


class TestAggregateResults:
    def test_empty_tuple(self) -> None:
        agg = aggregate_results(())
        assert agg == AggregateResult(
            total_score=0, max_possible=0, total_cost_usd=0.0, pass_rate=0.0
        )

    def test_single_task(self) -> None:
        t = _task_result(tests_passed=True, cost_usd=1.25, score=80)
        agg = aggregate_results((t,))
        assert agg.total_score == 80
        assert agg.max_possible == 100
        assert agg.total_cost_usd == pytest.approx(1.25)
        assert agg.pass_rate == pytest.approx(1.0)

    def test_multiple_tasks(self) -> None:
        t1 = _task_result(name="a", tests_passed=True, cost_usd=1.0, score=100)
        t2 = _task_result(name="b", tests_passed=False, cost_usd=2.0, score=20)
        t3 = _task_result(name="c", tests_passed=True, cost_usd=0.5, score=77)
        agg = aggregate_results((t1, t2, t3))
        assert agg.total_score == 197
        assert agg.max_possible == 300
        assert agg.total_cost_usd == pytest.approx(3.5)
        assert agg.pass_rate == pytest.approx(2.0 / 3.0)

    def test_all_failed(self) -> None:
        t1 = _task_result(tests_passed=False, cost_usd=0.1, score=0)
        t2 = _task_result(tests_passed=False, cost_usd=0.2, score=0)
        agg = aggregate_results((t1, t2))
        assert agg.pass_rate == pytest.approx(0.0)
        assert agg.total_score == 0


# ────────────────────────────────────────────────────────────────────────────
# build_benchmark_run
# ────────────────────────────────────────────────────────────────────────────


class TestBuildBenchmarkRun:
    def test_basic_build(self) -> None:
        t = _task_result(score=80, cost_usd=1.0, tests_passed=True)
        run = build_benchmark_run("2026-04-09T12:00:00", "abc123", (t,))
        assert run.run_id == "2026-04-09T12:00:00"
        assert run.harness_commit == "abc123"
        assert len(run.tasks) == 1
        assert run.aggregate.total_score == 80
        assert run.aggregate.max_possible == 100

    def test_empty_tasks(self) -> None:
        run = build_benchmark_run("run-empty", "def456", ())
        assert run.tasks == ()
        assert run.aggregate.total_score == 0
        assert run.aggregate.pass_rate == pytest.approx(0.0)

    def test_aggregate_matches_manual(self) -> None:
        t1 = _task_result(name="x", score=100, cost_usd=0.5, tests_passed=True)
        t2 = _task_result(name="y", score=60, cost_usd=1.5, tests_passed=False)
        run = build_benchmark_run("run-2", "commit-hash", (t1, t2))
        assert run.aggregate == aggregate_results((t1, t2))
