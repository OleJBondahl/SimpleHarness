"""Tests for pure functions in simpleharness.core."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from simpleharness.core import (
    Config,
    Permissions,
    SessionResult,
    State,
    Task,
    Workflow,
    _format_tool_call,
    _merge_config,
    _slugify,
    build_claude_cmd,
    build_session_prompt,
    compute_post_session_state,
    parse_frontmatter,
    pick_next_task,
    plan_tick,
    resolve_next_role,
    worksite_sh_dir,
)

# ── Factories ─────────────────────────────────────────────────────────────────


def _state(
    *,
    slug: str = "001-test",
    workflow: str = "default",
    status: str = "active",
    phase: str = "kickoff",
    last_role: str | None = None,
    next_role: str | None = None,
    total_sessions: int = 0,
    session_cap: int = 20,
    consecutive_same_role: int = 0,
    no_progress_ticks: int = 0,
    blocked_reason: str | None = None,
) -> State:
    return State(
        task_slug=slug,
        workflow=workflow,
        worksite="/fake/worksite",
        toolbox="/fake/toolbox",
        status=status,
        phase=phase,
        last_role=last_role,
        next_role=next_role,
        total_sessions=total_sessions,
        session_cap=session_cap,
        consecutive_same_role=consecutive_same_role,
        no_progress_ticks=no_progress_ticks,
        blocked_reason=blocked_reason,
        created="2024-01-01T00:00:00Z",
        updated="2024-01-01T00:00:00Z",
        last_session_id=None,
    )


def _task(
    *,
    slug: str = "001-test",
    state: State | None = None,
) -> Task:
    if state is None:
        state = _state(slug=slug)
    folder = Path(f"/fake/tasks/{slug}")
    return Task(
        slug=slug,
        folder=folder,
        task_md=folder / "TASK.md",
        state_path=folder / "STATE.md",
        state=state,
    )


def _workflow(
    *,
    name: str = "default",
    phases: tuple[str, ...] = ("developer",),
    max_sessions: int | None = None,
) -> Workflow:
    return Workflow(name=name, phases=phases, max_sessions=max_sessions)


def _config(**kwargs: Any) -> Config:
    return Config(**kwargs)


def _session(
    *,
    completed: bool = True,
    interrupted: bool = False,
    session_id: str = "sess-abc",
) -> SessionResult:
    return SessionResult(
        completed=completed,
        interrupted=interrupted,
        session_id=session_id,
        result_text=None,
        exit_code=0,
    )


_NOW = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)


# ── Frozen dataclasses ────────────────────────────────────────────────────────


def test_state_is_frozen():
    s = _state()
    with pytest.raises(dataclasses.FrozenInstanceError):
        cast(Any, s).status = "blocked"


def test_workflow_is_frozen():
    w = _workflow()
    with pytest.raises(dataclasses.FrozenInstanceError):
        cast(Any, w).name = "other"


def test_task_is_frozen():
    t = _task()
    with pytest.raises(dataclasses.FrozenInstanceError):
        cast(Any, t).slug = "other"


def test_session_result_is_frozen():
    sr = _session()
    with pytest.raises(dataclasses.FrozenInstanceError):
        cast(Any, sr).completed = False


# ── plan_tick: no_tasks ───────────────────────────────────────────────────────


def test_plan_tick_no_tasks():
    plan = plan_tick((), {}, frozenset(), _config())
    assert plan.kind == "no_tasks"


# ── plan_tick: no_active ──────────────────────────────────────────────────────


def test_plan_tick_no_active_all_done():
    t = _task(state=_state(status="done"))
    plan = plan_tick((t,), {}, frozenset(), _config())
    assert plan.kind == "no_active"


def test_plan_tick_no_active_all_blocked():
    t = _task(state=_state(status="blocked"))
    plan = plan_tick((t,), {}, frozenset(), _config())
    assert plan.kind == "no_active"


# ── plan_tick: block ──────────────────────────────────────────────────────────


def test_plan_tick_session_cap_block():
    t = _task(state=_state(total_sessions=20, session_cap=20))
    wf = _workflow()
    plan = plan_tick((t,), {"default": wf}, frozenset(), _config())
    assert plan.kind == "block"
    assert plan.block_task_slug == t.slug
    assert "session cap" in (plan.block_reason or "")


def test_plan_tick_workflow_missing_block():
    t = _task(state=_state(workflow="missing-wf"))
    plan = plan_tick((t,), {"missing-wf": None}, frozenset(), _config())
    assert plan.kind == "block"
    assert plan.block_task_slug == t.slug
    assert "workflow load failed" in (plan.block_reason or "")


def test_plan_tick_correction_pending_no_phases_block():
    t = _task(state=_state(last_role=None))
    wf = _workflow(phases=())
    plan = plan_tick((t,), {"default": wf}, frozenset({t.slug}), _config())
    assert plan.kind == "block"
    assert "no phases" in (plan.block_reason or "")


def test_plan_tick_no_phases_no_correction_block():
    t = _task(state=_state(last_role=None))
    wf = _workflow(phases=())
    plan = plan_tick((t,), {"default": wf}, frozenset(), _config())
    assert plan.kind == "block"
    assert "no phases" in (plan.block_reason or "")


# ── plan_tick: run ────────────────────────────────────────────────────────────


def test_plan_tick_normal_run():
    t = _task(state=_state(last_role=None))
    wf = _workflow(phases=("developer",))
    plan = plan_tick((t,), {"default": wf}, frozenset(), _config())
    assert plan.kind == "run"
    assert plan.run_task_slug == t.slug
    assert plan.run_role_name == "developer"


def test_plan_tick_correction_pending_reruns_last_role():
    t = _task(state=_state(last_role="reviewer"))
    wf = _workflow(phases=("developer", "reviewer"))
    plan = plan_tick((t,), {"default": wf}, frozenset({t.slug}), _config())
    assert plan.kind == "run"
    assert plan.run_role_name == "reviewer"


def test_plan_tick_correction_pending_no_last_role_uses_first_phase():
    t = _task(state=_state(last_role=None))
    wf = _workflow(phases=("developer", "reviewer"))
    plan = plan_tick((t,), {"default": wf}, frozenset({t.slug}), _config())
    assert plan.kind == "run"
    assert plan.run_role_name == "developer"


def test_plan_tick_past_final_phase_loopback():
    # last_role == final phase → resolve_next_role returns None → loopback
    t = _task(state=_state(last_role="reviewer"))
    wf = _workflow(phases=("developer", "reviewer"))
    plan = plan_tick((t,), {"default": wf}, frozenset(), _config())
    assert plan.kind == "run"
    # loopback to last_role (reviewer) since no fallback uses last_role first
    assert plan.run_role_name == "reviewer"


def test_plan_tick_next_role_override_respected():
    t = _task(state=_state(next_role="project-leader"))
    wf = _workflow(phases=("developer",))
    plan = plan_tick((t,), {"default": wf}, frozenset(), _config())
    assert plan.kind == "run"
    assert plan.run_role_name == "project-leader"


def test_plan_tick_priority_correction_over_alphabetical():
    # t2 is alphabetically first; t1 has correction → t1 gets picked
    t1 = _task(slug="002-beta", state=_state(slug="002-beta"))
    t2 = _task(slug="001-alpha", state=_state(slug="001-alpha"))
    wf = _workflow()
    plan = plan_tick((t1, t2), {"default": wf}, frozenset({"002-beta"}), _config())
    assert plan.kind == "run"
    assert plan.run_task_slug == "002-beta"


# ── compute_post_session_state: all transitions ───────────────────────────────


def test_compute_post_session_increments_total_sessions():
    state = _state(total_sessions=3)
    new = compute_post_session_state(
        state, "developer", _session(), None, 0, "pre", "post", _config(), _NOW
    )
    assert new.total_sessions == 4


def test_compute_post_session_sets_last_role():
    state = _state()
    new = compute_post_session_state(
        state, "reviewer", _session(), None, 0, "pre", "post", _config(), _NOW
    )
    assert new.last_role == "reviewer"


def test_compute_post_session_sets_session_id():
    state = _state()
    new = compute_post_session_state(
        state, "developer", _session(session_id="my-id"), None, 0, "pre", "post", _config(), _NOW
    )
    assert new.last_session_id == "my-id"


def test_compute_post_session_sets_updated():
    state = _state()
    new = compute_post_session_state(
        state, "developer", _session(), None, 0, "pre", "post", _config(), _NOW
    )
    assert new.updated == "2024-06-15T12:00:00Z"


def test_compute_post_session_progress_resets_no_progress():
    state = _state(no_progress_ticks=3)
    new = compute_post_session_state(
        state, "developer", _session(), None, 0, "pre", "post", _config(), _NOW
    )
    assert new.no_progress_ticks == 0


def test_compute_post_session_no_progress_increments():
    state = _state(no_progress_ticks=2)
    new = compute_post_session_state(
        state, "developer", _session(), None, 0, "same-hash", "same-hash", _config(), _NOW
    )
    assert new.no_progress_ticks == 3


def test_compute_post_session_consecutive_same_role_increments():
    state = _state(consecutive_same_role=2)
    new = compute_post_session_state(
        state, "developer", _session(), "developer", 2, "pre", "post", _config(), _NOW
    )
    assert new.consecutive_same_role == 3


def test_compute_post_session_consecutive_resets_on_role_change():
    state = _state(consecutive_same_role=2)
    new = compute_post_session_state(
        state, "reviewer", _session(), "developer", 2, "pre", "post", _config(), _NOW
    )
    assert new.consecutive_same_role == 1


def test_compute_post_session_cap_hit_blocks():
    # session_cap=5, total_sessions=4 → after increment = 5 → blocked
    state = _state(total_sessions=4, session_cap=5)
    new = compute_post_session_state(
        state, "developer", _session(), None, 0, "pre", "post", _config(), _NOW
    )
    assert new.status == "blocked"
    assert "session cap" in (new.blocked_reason or "")


def test_compute_post_session_repeat_limit_blocks():
    cfg = _config(max_same_role_repeats=3)
    state = _state(consecutive_same_role=2)
    new = compute_post_session_state(
        state, "developer", _session(), "developer", 2, "pre", "post", cfg, _NOW
    )
    # consecutive_same_role → 3 which equals max_same_role_repeats=3 → blocked
    assert new.status == "blocked"
    assert "developer" in (new.blocked_reason or "")
    assert "3 times" in (new.blocked_reason or "")


def test_compute_post_session_cap_takes_priority_over_repeat():
    # Both cap and repeat limit hit simultaneously — cap check runs first
    cfg = _config(max_same_role_repeats=2)
    state = _state(total_sessions=4, session_cap=5, consecutive_same_role=1)
    new = compute_post_session_state(
        state, "developer", _session(), "developer", 1, "pre", "post", cfg, _NOW
    )
    assert new.status == "blocked"
    assert "session cap" in (new.blocked_reason or "")


def test_compute_post_session_no_block_below_limits():
    cfg = _config(max_same_role_repeats=3)
    state = _state(total_sessions=1, session_cap=20, consecutive_same_role=1)
    new = compute_post_session_state(
        state, "developer", _session(), "developer", 1, "pre", "post", cfg, _NOW
    )
    assert new.status == "active"
    assert new.blocked_reason is None


def test_compute_post_session_output_is_frozen():
    state = _state()
    new = compute_post_session_state(
        state, "developer", _session(), None, 0, "pre", "post", _config(), _NOW
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        cast(Any, new).status = "done"


# ── pick_next_task ────────────────────────────────────────────────────────────


def test_pick_next_task_returns_none_if_empty():
    assert pick_next_task([], frozenset()) is None


def test_pick_next_task_returns_none_if_all_inactive():
    t = _task(state=_state(status="done"))
    assert pick_next_task([t], frozenset()) is None


def test_pick_next_task_alphabetical():
    t1 = _task(slug="002-b")
    t2 = _task(slug="001-a")
    result = pick_next_task([t1, t2], frozenset())
    assert result is not None
    assert result.slug == "001-a"


def test_pick_next_task_correction_priority():
    t1 = _task(slug="001-a")
    t2 = _task(slug="002-b")
    result = pick_next_task([t1, t2], frozenset({"002-b"}))
    assert result is not None
    assert result.slug == "002-b"


# ── resolve_next_role ─────────────────────────────────────────────────────────


def test_resolve_next_role_inactive_returns_none():
    t = _task(state=_state(status="blocked"))
    wf = _workflow(phases=("developer",))
    assert resolve_next_role(t, wf) is None


def test_resolve_next_role_next_role_override():
    t = _task(state=_state(next_role="project-leader"))
    wf = _workflow(phases=("developer",))
    assert resolve_next_role(t, wf) == "project-leader"


def test_resolve_next_role_first_phase_when_no_last():
    t = _task(state=_state(last_role=None))
    wf = _workflow(phases=("developer", "reviewer"))
    assert resolve_next_role(t, wf) == "developer"


def test_resolve_next_role_advances():
    t = _task(state=_state(last_role="developer"))
    wf = _workflow(phases=("developer", "reviewer"))
    assert resolve_next_role(t, wf) == "reviewer"


def test_resolve_next_role_past_final_returns_none():
    t = _task(state=_state(last_role="reviewer"))
    wf = _workflow(phases=("developer", "reviewer"))
    assert resolve_next_role(t, wf) is None


def test_resolve_next_role_last_role_not_in_phases_restarts():
    t = _task(state=_state(last_role="unknown-role"))
    wf = _workflow(phases=("developer", "reviewer"))
    assert resolve_next_role(t, wf) == "developer"


def test_resolve_next_role_empty_phases_returns_none():
    t = _task(state=_state())
    wf = _workflow(phases=())
    assert resolve_next_role(t, wf) is None


# ── parse_frontmatter ─────────────────────────────────────────────────────────


def test_parse_frontmatter_with_frontmatter():
    text = "---\nname: foo\n---\nbody text"
    meta, body = parse_frontmatter(text)
    assert meta == {"name": "foo"}
    assert body == "body text"


def test_parse_frontmatter_no_frontmatter():
    text = "just plain text"
    meta, body = parse_frontmatter(text)
    assert meta == {}
    assert body == "just plain text"


def test_parse_frontmatter_invalid_yaml():
    text = "---\n: invalid: yaml: here\n---\nbody"
    with pytest.raises(ValueError, match="invalid YAML frontmatter"):
        parse_frontmatter(text)


# ── _merge_config ─────────────────────────────────────────────────────────────


def test_merge_config_simple():
    base = {"model": "opus", "max_turns": 60}
    override = {"model": "sonnet"}
    result = _merge_config(base, override)
    assert result["model"] == "sonnet"
    assert result["max_turns"] == 60


def test_merge_config_nested():
    base = {"permissions": {"mode": "safe", "extra": []}}
    override = {"permissions": {"mode": "approver"}}
    result = _merge_config(base, override)
    assert result["permissions"]["mode"] == "approver"
    assert result["permissions"]["extra"] == []


def test_merge_config_empty_override():
    base = {"a": 1, "b": 2}
    result = _merge_config(base, {})
    assert result == {"a": 1, "b": 2}


# ── _slugify ──────────────────────────────────────────────────────────────────


def test_slugify_basic():
    assert _slugify("Hello World") == "hello-world"


def test_slugify_special_chars():
    assert _slugify("Fix bug: #123!") == "fix-bug-123"


def test_slugify_empty():
    assert _slugify("") == "task"


def test_slugify_truncates():
    long = "a" * 100
    result = _slugify(long)
    assert len(result) <= 60


# ── _format_tool_call ─────────────────────────────────────────────────────────


def test_format_tool_call_bash():
    result = _format_tool_call("Bash", {"command": "ls -la"})
    assert result == "$ ls -la"


def test_format_tool_call_read():
    result = _format_tool_call("Read", {"file_path": "/tmp/x.py"})
    assert result == "/tmp/x.py"


def test_format_tool_call_write():
    result = _format_tool_call("Write", {"file_path": "/tmp/out.py"})
    assert result == "/tmp/out.py"


def test_format_tool_call_agent():
    result = _format_tool_call("Agent", {"model": "haiku", "description": "search files"})
    assert "[haiku]" in result
    assert "search files" in result


def test_format_tool_call_unknown():
    result = _format_tool_call("UnknownTool", {"key": "value"})
    assert "value" in result


# ── worksite_sh_dir ───────────────────────────────────────────────────────────


def test_worksite_sh_dir():
    result = worksite_sh_dir(Path("/my/project"))
    assert result == Path("/my/project/simpleharness")


# ── build_session_prompt ──────────────────────────────────────────────────────


def test_build_session_prompt_contains_role_and_workflow():
    from simpleharness.core import Role

    task = _task()
    role = Role(name="developer", body="you are a developer")
    wf = _workflow(phases=("developer",))
    prompt = build_session_prompt(task, role, wf, Path("/toolbox"), None, [])
    assert "developer" in prompt
    assert "default" in prompt
    assert "SimpleHarness" in prompt


def test_build_session_prompt_with_correction():
    from simpleharness.core import Role

    task = _task()
    role = Role(name="developer", body="dev")
    wf = _workflow()
    prompt = build_session_prompt(task, role, wf, Path("/toolbox"), "Fix the bug!", [])
    assert "USER INTERVENTION" in prompt
    assert "Fix the bug!" in prompt


# ── build_claude_cmd ──────────────────────────────────────────────────────────


def test_build_claude_cmd_safe_mode():
    from simpleharness.core import Role

    role = Role(name="developer", body="dev")
    cfg = Config(permissions=Permissions(mode="safe"))
    cmd = build_claude_cmd(
        Path("/task/.session_prompt.md"),
        role,
        Path("/toolbox"),
        "sess-123",
        cfg,
    )
    assert "claude" in cmd
    assert "--permission-mode" in cmd
    assert "acceptEdits" in cmd
    assert "--allowedTools" in cmd


def test_build_claude_cmd_dangerous_mode():
    from simpleharness.core import Role

    role = Role(name="developer", body="dev")
    cfg = Config(permissions=Permissions(mode="dangerous"))
    cmd = build_claude_cmd(
        Path("/task/.session_prompt.md"),
        role,
        Path("/toolbox"),
        "sess-456",
        cfg,
    )
    assert "bypassPermissions" in cmd


def test_build_claude_cmd_session_id_included():
    from simpleharness.core import Role

    role = Role(name="developer", body="dev")
    cfg = Config()
    cmd = build_claude_cmd(
        Path("/task/.session_prompt.md"),
        role,
        Path("/toolbox"),
        "my-session-id",
        cfg,
    )
    assert "--session-id" in cmd
    assert "my-session-id" in cmd
