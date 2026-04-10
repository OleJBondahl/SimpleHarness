"""Exercise every @deal.pure / @deal.chain function with deal enabled.

Catches runtime contract violations that static deal-lint misses:
- SilentContractError (stdout/stderr writes from within pure functions)
- RaisesContractError (unexpected exceptions from deal.safe)
- Reentrancy bugs (recursive @deal.pure corrupting HasPatcher state)

Usage: uv run python scripts/check_deal_runtime.py
Exits 0 if all functions pass. Exits 1 with a list of failures.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import deal

# Enable deal runtime checking explicitly — this is the whole point.
deal.enable()

from simpleharness.core import (  # noqa: E402, I001
    ClassifyResult,
    Config,
    Deliverable,
    Role,
    SessionResult,
    SkillList,
    State,
    Subagent,
    Task,
    TaskSpec,
    Workflow,
    _build_allowlist,
    _format_tool_call,
    _merge_config,
    _parse_retry_after,
    _slugify,
    build_claude_cmd,
    build_exported_subagent_file,
    build_refinement_text,
    build_rebrief_text,
    build_session_env,
    build_session_hooks_config,
    build_session_prompt,
    build_subagent_export_body,
    build_subagent_export_frontmatter,
    check_deliverables,
    classify_cli_error,
    compute_backoff_delay,
    compute_post_session_state,
    deps_satisfied,
    format_task_dashboard,
    merge_skill_lists,
    parse_frontmatter,
    parse_skill_list,
    parse_task_spec,
    pause_file_path,
    pick_next_task,
    plan_downstream_transitions,
    plan_tick,
    resolve_next_role,
    toolbox_root,
    worksite_sh_dir,
)
from simpleharness.approver_core import (  # noqa: E402
    ApproverEnv,
    Verdict,
    _deny_synthetic,
    _extract_assistant_text,
    _task_dir,
    build_approver_prompt,
    command_signature,
    fake_verdict_from_input,
    finalize_review,
    parse_verdict,
    plan_review,
    unwrap_wrappers,
)


# ── Test fixtures ─────────────────────────────────────────────────────────────

_NOW = datetime(2026, 1, 1, tzinfo=UTC)

_ROLE = Role(name="reviewer", body="You review code.")
_SKILL_LIST = SkillList()
_CONFIG = Config()
_WORKFLOW = Workflow(name="default", phases=("plan", "implement", "review"))
_SUBAGENT = Subagent(name="helper", body="You help.")
_DELIVERABLE = Deliverable(path="output.md")
_TASK_SPEC = TaskSpec(title="Test task", workflow="default")
_STATE = State(
    task_slug="test-task",
    workflow="default",
    worksite="/tmp/ws",
    toolbox="/tmp/tb",
)
_TASK = Task(
    slug="test-task",
    folder=Path("/tmp/tasks/test-task"),
    task_md=Path("/tmp/tasks/test-task/TASK.md"),
    state_path=Path("/tmp/tasks/test-task/STATE.md"),
    state=_STATE,
    spec=_TASK_SPEC,
)
_SESSION = SessionResult(
    completed=True,
    interrupted=False,
    session_id="sess-001",
    result_text="done",
    exit_code=0,
)
_CLASSIFY = ClassifyResult(outcome="transient", reason="network error")
_VERDICT = Verdict(decision="allow", pattern="echo *", reason="safe")
_APPROVER_ENV = ApproverEnv(
    worksite=Path("/tmp/ws"),
    task_slug="test-task",
    role="reviewer",
    approver_model="sonnet",
    stream_log=None,
    fake=True,
    timeout_s=30.0,
)


# ── Test cases ────────────────────────────────────────────────────────────────


from collections.abc import Callable  # noqa: E402


def _test_cases() -> list[tuple[str, Callable[..., object], tuple, dict]]:
    """Return (label, function, args, kwargs) for every pure function."""
    return [
        # ── core.py ───────────────────────────────────────────────────────
        ("parse_frontmatter", parse_frontmatter, ("---\ntitle: x\n---\nbody",), {}),
        ("toolbox_root", toolbox_root, (), {}),
        ("_merge_config flat", _merge_config, ({"a": 1}, {"b": 2}), {}),
        (
            "_merge_config nested",
            _merge_config,
            ({"p": {"mode": "safe"}}, {"p": {"mode": "approver"}}),
            {},
        ),
        (
            "_merge_config deep nested",
            _merge_config,
            ({"a": {"b": {"c": 1}}}, {"a": {"b": {"c": 2, "d": 3}}}),
            {},
        ),
        ("parse_skill_list None", parse_skill_list, (None,), {}),
        (
            "parse_skill_list dict",
            parse_skill_list,
            ({"available": [{"name": "foo", "hint": "bar"}], "must_use": ["foo"]},),
            {},
        ),
        ("merge_skill_lists", merge_skill_lists, (_SKILL_LIST, _SKILL_LIST), {}),
        ("worksite_sh_dir", worksite_sh_dir, (Path("/tmp/ws"),), {}),
        ("pause_file_path", pause_file_path, (Path("/tmp/ws"),), {}),
        ("parse_task_spec", parse_task_spec, ({"title": "t", "workflow": "default"},), {}),
        ("deps_satisfied", deps_satisfied, (_TASK_SPEC, {"test-task": "done"}), {}),
        (
            "plan_downstream_transitions",
            plan_downstream_transitions,
            (
                "done-task",
                {"other": TaskSpec(title="o", workflow="default", depends_on=("done-task",))},
            ),
            {},
        ),
        ("build_refinement_text", build_refinement_text, ("done-task", (_DELIVERABLE,)), {}),
        ("build_rebrief_text", build_rebrief_text, ("done-task", "next-task", (_DELIVERABLE,)), {}),
        ("check_deliverables", check_deliverables, (_TASK_SPEC, frozenset()), {}),
        ("_parse_retry_after valid", _parse_retry_after, ("2026-01-01T00:00:00+00:00",), {}),
        ("_parse_retry_after invalid", _parse_retry_after, ("bad",), {}),
        ("pick_next_task", pick_next_task, ((_TASK,), frozenset(), _NOW), {}),
        ("resolve_next_role", resolve_next_role, (_TASK, _WORKFLOW), {}),
        (
            "build_session_prompt",
            build_session_prompt,
            (_TASK, _ROLE, _WORKFLOW, Path("/tmp/tb"), None, []),
            {},
        ),
        ("_build_allowlist", _build_allowlist, (_ROLE, _CONFIG), {}),
        (
            "build_claude_cmd",
            build_claude_cmd,
            (Path("/tmp/prompt.md"), _ROLE, Path("/tmp/tb"), "sess-001", _CONFIG),
            {},
        ),
        ("_format_tool_call", _format_tool_call, ("Bash", {"command": "echo hi"}), {}),
        ("build_subagent_export_body", build_subagent_export_body, (_SUBAGENT,), {}),
        ("build_subagent_export_frontmatter", build_subagent_export_frontmatter, (_SUBAGENT,), {}),
        ("build_exported_subagent_file", build_exported_subagent_file, (_SUBAGENT,), {}),
        ("build_session_hooks_config off", build_session_hooks_config, ("off", "python"), {}),
        ("build_session_hooks_config on", build_session_hooks_config, ("warn", "python"), {}),
        ("build_session_env", build_session_env, ({}, _ROLE, (_SUBAGENT,), _CONFIG), {}),
        ("_slugify", _slugify, ("Hello World!",), {}),
        (
            "plan_tick",
            plan_tick,
            ((_TASK,), {"default": _WORKFLOW}, frozenset(), _CONFIG, _NOW),
            {},
        ),
        (
            "compute_post_session_state",
            compute_post_session_state,
            (_STATE, "reviewer", _SESSION, None, 0, "aaa", "bbb", _CONFIG, _NOW),
            {"classify_result": _CLASSIFY},
        ),
        ("compute_backoff_delay", compute_backoff_delay, (0,), {}),
        ("classify_cli_error", classify_cli_error, (1, "error text"), {}),
        (
            "format_task_dashboard",
            format_task_dashboard,
            (_STATE, ("plan", "implement", "review")),
            {},
        ),
        # ── approver_core.py ──────────────────────────────────────────────
        ("unwrap_wrappers", unwrap_wrappers, (["sudo", "ls", "-la"],), {}),
        ("command_signature", command_signature, ("echo hello",), {}),
        ("_deny_synthetic", _deny_synthetic, ("test reason",), {}),
        (
            "parse_verdict valid",
            parse_verdict,
            ('```json\n{"decision": "allow", "pattern": "echo *", "reason": "safe"}\n```',),
            {},
        ),
        ("parse_verdict invalid", parse_verdict, ("garbage",), {}),
        (
            "build_approver_prompt",
            build_approver_prompt,
            ("Bash", {"command": "ls"}, "reviewer", "test-task", "tail", []),
            {},
        ),
        (
            "fake_verdict_from_input Bash",
            fake_verdict_from_input,
            ("Bash", {"command": "echo hi"}),
            {},
        ),
        (
            "fake_verdict_from_input other",
            fake_verdict_from_input,
            ("Read", {"path": "/tmp/f"}),
            {},
        ),
        ("_extract_assistant_text", _extract_assistant_text, ("hello",), {}),
        ("_task_dir", _task_dir, (Path("/tmp/ws"), "test-task"), {}),
        (
            "plan_review fake",
            plan_review,
            (_APPROVER_ENV, "Bash", {"command": "ls"}, _CONFIG),
            {"role_file_exists": True, "stream_tail": "", "currently_approved": ()},
        ),
        ("finalize_review", finalize_review, (_VERDICT, _CONFIG), {}),
    ]


# ── Runner ────────────────────────────────────────────────────────────────────


def main() -> int:
    failures: list[str] = []
    cases = _test_cases()

    for label, func, args, kwargs in cases:
        try:
            func(*args, **kwargs)
        except deal.ContractError as e:
            failures.append(f"  {label}: {type(e).__name__}: {e}")
        except Exception as e:
            failures.append(f"  {label}: {type(e).__name__}: {e}")

    if failures:
        # Use stderr — stdout may be patched if a test corrupted it
        sys.stderr.write("deal runtime check: FAILURES\n")
        for f in failures:
            sys.stderr.write(f + "\n")
        return 1

    sys.stderr.write(f"deal runtime check: all {len(cases)} cases passed\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
