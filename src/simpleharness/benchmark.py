"""Benchmark shell — impure orchestration for benchmark runs.

Resets snapshot repos, runs the harness, collects test results, and writes
score reports. All pure scoring logic lives in benchmark_core.py.
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import yaml

from simpleharness.benchmark_core import (
    BenchmarkRun,
    TaskResult,
    build_benchmark_run,
    compute_task_score,
)
from simpleharness.core import parse_frontmatter

if TYPE_CHECKING:
    from pathlib import Path

log = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────────────
# Git helpers
# ────────────────────────────────────────────────────────────────────────────


def reset_repo(repo_path: Path) -> None:
    """Reset a snapshot repo to its benchmark-start state."""
    subprocess.run(
        ["git", "checkout", "benchmark-start"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "clean", "-fd"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=True,
    )


def get_harness_commit() -> str:
    """Return the short commit hash of the harness repo HEAD."""
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


# ────────────────────────────────────────────────────────────────────────────
# Test runner
# ────────────────────────────────────────────────────────────────────────────


def run_tests(repo_path: Path, test_command: str) -> tuple[bool, str]:
    """Run a test command in the repo and return (passed, output)."""
    parts = shlex.split(test_command)
    result = subprocess.run(
        parts,
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )
    combined = result.stdout + result.stderr
    return (result.returncode == 0, combined)


# ────────────────────────────────────────────────────────────────────────────
# File I/O helpers
# ────────────────────────────────────────────────────────────────────────────


def load_benchmark_meta(repo_path: Path) -> dict[str, Any]:
    """Read and parse benchmark-meta.yaml from a repo."""
    meta_file = repo_path / "benchmark-meta.yaml"
    text = meta_file.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        msg = f"benchmark-meta.yaml must be a mapping, got {type(data).__name__}"
        raise ValueError(msg)
    return data


def collect_state_metrics(state_path: Path) -> dict[str, Any]:
    """Extract metrics from a STATE.md frontmatter block.

    Returns sensible defaults if the file does not exist.
    """
    defaults: dict[str, Any] = {
        "total_sessions": 0,
        "total_cost_usd": 0.0,
        "no_progress_ticks": 0,
        "duration_seconds": 0.0,
    }
    if not state_path.exists():
        return defaults
    text = state_path.read_text(encoding="utf-8")
    meta, _body = parse_frontmatter(text)
    return {**defaults, **meta}


def check_deliverables(repo_path: Path, task_path: Path) -> bool:
    """Check whether all deliverables listed in TASK.md exist in the repo."""
    task_file = task_path / "TASK.md"
    if not task_file.exists():
        return False
    text = task_file.read_text(encoding="utf-8")
    meta, _body = parse_frontmatter(text)
    deliverables = meta.get("deliverables", [])
    if not deliverables:
        return True
    return all((repo_path / d).exists() for d in deliverables)


# ────────────────────────────────────────────────────────────────────────────
# Discovery
# ────────────────────────────────────────────────────────────────────────────


def discover_benchmark_repos(benchmarks_dir: Path) -> list[Path]:
    """Find subdirectories under benchmarks_dir/repos that contain benchmark-meta.yaml."""
    repos_dir = benchmarks_dir / "repos"
    if not repos_dir.is_dir():
        return []
    return sorted(
        d for d in repos_dir.iterdir() if d.is_dir() and (d / "benchmark-meta.yaml").exists()
    )


# ────────────────────────────────────────────────────────────────────────────
# Single benchmark run
# ────────────────────────────────────────────────────────────────────────────


def run_single_benchmark(repo_path: Path, task_name: str) -> TaskResult:
    """Run a single benchmark task and return the scored result.

    Currently runs tests directly against the repo without spawning a full
    harness session.
    """
    # TODO: integrate with harness tick_once
    meta = load_benchmark_meta(repo_path)
    task_type = meta.get("task_type", "unknown")
    language = meta.get("language", "unknown")
    test_command = meta.get("test_command", "uv run pytest")
    task_dir = repo_path / "simpleharness" / "tasks" / task_name

    reset_repo(repo_path)

    start = datetime.now(UTC)
    tests_passed, _test_output = run_tests(repo_path, test_command)
    duration = (datetime.now(UTC) - start).total_seconds()

    deliverables_present = check_deliverables(repo_path, task_dir)

    state_path = repo_path / "simpleharness" / "STATE.md"
    state_metrics = collect_state_metrics(state_path)
    total_sessions = state_metrics.get("total_sessions", 0)
    no_progress_ticks = state_metrics.get("no_progress_ticks", 0)
    cost_usd = state_metrics.get("total_cost_usd", 0.0)

    score = compute_task_score(
        tests_passed=tests_passed,
        tests_regression=False,
        total_sessions=total_sessions,
        no_progress_ticks=no_progress_ticks,
        deliverables_present=deliverables_present,
    )

    return TaskResult(
        name=task_name,
        task_type=task_type,
        language=language,
        tests_passed=tests_passed,
        tests_regression=False,
        cost_usd=cost_usd,
        total_sessions=total_sessions,
        no_progress_ticks=no_progress_ticks,
        deliverables_present=deliverables_present,
        duration_seconds=duration,
        score=score,
    )


# ────────────────────────────────────────────────────────────────────────────
# Full benchmark orchestration
# ────────────────────────────────────────────────────────────────────────────


def run_all_benchmarks(benchmarks_dir: Path) -> BenchmarkRun:
    """Discover all benchmark repos, run each, and return a BenchmarkRun."""
    repos = discover_benchmark_repos(benchmarks_dir)
    run_id = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S")
    harness_commit = get_harness_commit()

    results: list[TaskResult] = []
    for repo_path in repos:
        task_name = repo_path.name
        try:
            result = run_single_benchmark(repo_path, task_name)
            results.append(result)
        except Exception:
            log.exception("benchmark failed for %s", task_name)

    return build_benchmark_run(
        run_id=run_id,
        harness_commit=harness_commit,
        task_results=tuple(results),
    )


# ────────────────────────────────────────────────────────────────────────────
# Output
# ────────────────────────────────────────────────────────────────────────────


def write_results(run: BenchmarkRun, output_dir: Path) -> Path:
    """Serialize a BenchmarkRun to JSON and write to output_dir."""
    output_dir.mkdir(parents=True, exist_ok=True)
    out_file = output_dir / f"{run.run_id}.json"
    out_file.write_text(json.dumps(asdict(run), indent=2), encoding="utf-8")
    return out_file


def format_summary(run: BenchmarkRun) -> str:
    """Return a human-readable summary of a benchmark run."""
    lines: list[str] = []
    lines.append(f"Benchmark Run: {run.run_id}")
    lines.append(f"Harness Commit: {run.harness_commit}")
    lines.append("")
    lines.append(f"{'Task':<30} {'Pass':>6} {'Score':>6} {'Cost':>10}")
    lines.append("-" * 56)
    for t in run.tasks:
        status = "PASS" if t.tests_passed else "FAIL"
        lines.append(f"{t.name:<30} {status:>6} {t.score:>6} ${t.cost_usd:>9.4f}")
    lines.append("-" * 56)
    agg = run.aggregate
    lines.append(
        f"{'Total':<30} {agg.pass_rate:>5.0%} {agg.total_score:>5}/{agg.max_possible}"
        f" ${agg.total_cost_usd:>9.4f}"
    )
    return "\n".join(lines)
