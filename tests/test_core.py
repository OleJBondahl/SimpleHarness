"""Tests for pure functions in simpleharness.core."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from simpleharness.core import (
    DEFAULT_BACKOFF_SCHEDULE,
    ClassifyResult,
    Config,
    Deliverable,
    DownstreamAction,
    Permissions,
    SessionResult,
    State,
    Task,
    TaskSpec,
    Workflow,
    _format_tool_call,
    _merge_config,
    _slugify,
    build_claude_cmd,
    build_rebrief_text,
    build_refinement_text,
    build_session_prompt,
    check_deliverables,
    classify_cli_error,
    compute_backoff_delay,
    compute_post_session_state,
    deps_satisfied,
    format_task_dashboard,
    parse_frontmatter,
    parse_task_spec,
    pause_file_path,
    pick_next_task,
    plan_downstream_transitions,
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
    total_cost_usd: float = 0.0,
    retry_count: int = 0,
    retry_after: str | None = None,
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
        total_cost_usd=total_cost_usd,
        retry_count=retry_count,
        retry_after=retry_after,
    )


def _task(
    *,
    slug: str = "001-test",
    state: State | None = None,
    spec: TaskSpec | None = None,
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
        spec=spec,
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
    result_text: str | None = None,
    exit_code: int = 0,
    cost_usd: float | None = None,
    duration_ms: int | None = None,
) -> SessionResult:
    return SessionResult(
        completed=completed,
        interrupted=interrupted,
        session_id=session_id,
        result_text=result_text,
        exit_code=exit_code,
        cost_usd=cost_usd,
        duration_ms=duration_ms,
    )


_NOW = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)
_FAR_PAST = datetime(2000, 1, 1, tzinfo=UTC)


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
    plan = plan_tick((), {}, frozenset(), _config(), _FAR_PAST)
    assert plan.kind == "no_tasks"


# ── plan_tick: no_active ──────────────────────────────────────────────────────


def test_plan_tick_no_active_all_done():
    t = _task(state=_state(status="done"))
    plan = plan_tick((t,), {}, frozenset(), _config(), _FAR_PAST)
    assert plan.kind == "no_active"


def test_plan_tick_no_active_all_blocked():
    t = _task(state=_state(status="blocked"))
    plan = plan_tick((t,), {}, frozenset(), _config(), _FAR_PAST)
    assert plan.kind == "no_active"


# ── plan_tick: block ──────────────────────────────────────────────────────────


def test_plan_tick_session_cap_block():
    t = _task(state=_state(total_sessions=20, session_cap=20))
    wf = _workflow()
    plan = plan_tick((t,), {"default": wf}, frozenset(), _config(), _FAR_PAST)
    assert plan.kind == "block"
    assert plan.block_task_slug == t.slug
    assert "session cap" in (plan.block_reason or "")


def test_plan_tick_workflow_missing_block():
    t = _task(state=_state(workflow="missing-wf"))
    plan = plan_tick((t,), {"missing-wf": None}, frozenset(), _config(), _FAR_PAST)
    assert plan.kind == "block"
    assert plan.block_task_slug == t.slug
    assert "workflow load failed" in (plan.block_reason or "")


def test_plan_tick_correction_pending_no_phases_block():
    t = _task(state=_state(last_role=None))
    wf = _workflow(phases=())
    plan = plan_tick((t,), {"default": wf}, frozenset({t.slug}), _config(), _FAR_PAST)
    assert plan.kind == "block"
    assert "no phases" in (plan.block_reason or "")


def test_plan_tick_no_phases_no_correction_block():
    t = _task(state=_state(last_role=None))
    wf = _workflow(phases=())
    plan = plan_tick((t,), {"default": wf}, frozenset(), _config(), _FAR_PAST)
    assert plan.kind == "block"
    assert "no phases" in (plan.block_reason or "")


# ── plan_tick: run ────────────────────────────────────────────────────────────


def test_plan_tick_normal_run():
    t = _task(state=_state(last_role=None))
    wf = _workflow(phases=("developer",))
    plan = plan_tick((t,), {"default": wf}, frozenset(), _config(), _FAR_PAST)
    assert plan.kind == "run"
    assert plan.run_task_slug == t.slug
    assert plan.run_role_name == "developer"


def test_plan_tick_correction_pending_reruns_last_role():
    t = _task(state=_state(last_role="reviewer"))
    wf = _workflow(phases=("developer", "reviewer"))
    plan = plan_tick((t,), {"default": wf}, frozenset({t.slug}), _config(), _FAR_PAST)
    assert plan.kind == "run"
    assert plan.run_role_name == "reviewer"


def test_plan_tick_correction_pending_no_last_role_uses_first_phase():
    t = _task(state=_state(last_role=None))
    wf = _workflow(phases=("developer", "reviewer"))
    plan = plan_tick((t,), {"default": wf}, frozenset({t.slug}), _config(), _FAR_PAST)
    assert plan.kind == "run"
    assert plan.run_role_name == "developer"


def test_plan_tick_past_final_phase_loopback():
    # last_role == final phase → resolve_next_role returns None → loopback
    t = _task(state=_state(last_role="reviewer"))
    wf = _workflow(phases=("developer", "reviewer"))
    plan = plan_tick((t,), {"default": wf}, frozenset(), _config(), _FAR_PAST)
    assert plan.kind == "run"
    # loopback to last_role (reviewer) since no fallback uses last_role first
    assert plan.run_role_name == "reviewer"


def test_plan_tick_next_role_override_respected():
    t = _task(state=_state(next_role="project-leader"))
    wf = _workflow(phases=("developer",))
    plan = plan_tick((t,), {"default": wf}, frozenset(), _config(), _FAR_PAST)
    assert plan.kind == "run"
    assert plan.run_role_name == "project-leader"


def test_plan_tick_priority_correction_over_alphabetical():
    # t2 is alphabetically first; t1 has correction → t1 gets picked
    t1 = _task(slug="002-beta", state=_state(slug="002-beta"))
    t2 = _task(slug="001-alpha", state=_state(slug="001-alpha"))
    wf = _workflow()
    plan = plan_tick((t1, t2), {"default": wf}, frozenset({"002-beta"}), _config(), _FAR_PAST)
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


def test_compute_post_session_state_accumulates_cost():
    state = _state(total_cost_usd=1.50)
    session = _session(cost_usd=0.75)
    new = compute_post_session_state(state, "dev", session, None, 0, "pre", "post", _config(), _NOW)
    assert new.total_cost_usd == pytest.approx(2.25)


def test_compute_post_session_state_none_cost():
    state = _state(total_cost_usd=1.00)
    session = _session(cost_usd=None)
    new = compute_post_session_state(state, "dev", session, None, 0, "pre", "post", _config(), _NOW)
    assert new.total_cost_usd == pytest.approx(1.00)


# ── pick_next_task ────────────────────────────────────────────────────────────


def test_pick_next_task_returns_none_if_empty():
    assert pick_next_task([], frozenset(), _FAR_PAST) is None


def test_pick_next_task_returns_none_if_all_inactive():
    t = _task(state=_state(status="done"))
    assert pick_next_task([t], frozenset(), _FAR_PAST) is None


def test_pick_next_task_alphabetical():
    t1 = _task(slug="002-b")
    t2 = _task(slug="001-a")
    result = pick_next_task([t1, t2], frozenset(), _FAR_PAST)
    assert result is not None
    assert result.slug == "001-a"


def test_pick_next_task_correction_priority():
    t1 = _task(slug="001-a")
    t2 = _task(slug="002-b")
    result = pick_next_task([t1, t2], frozenset({"002-b"}), _FAR_PAST)
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


def test_build_session_prompt_with_phase_previews():
    from simpleharness.core import Role

    task = _task()
    role = Role(name="developer", body="dev")
    wf = _workflow()
    phase_files = [Path("/task/00-kickoff.md"), Path("/task/01-brainstorm.md")]
    previews = {
        "00-kickoff.md": "# Kickoff\nTask is ready.",
        "01-brainstorm.md": "# Brainstorm\nThree approaches considered.",
    }
    prompt = build_session_prompt(task, role, wf, Path("/toolbox"), None, phase_files, previews)
    assert "00-kickoff.md" in prompt
    assert "# Kickoff" in prompt
    assert "Task is ready." in prompt
    assert "Three approaches considered." in prompt


def test_build_session_prompt_without_previews_still_works():
    from simpleharness.core import Role

    task = _task()
    role = Role(name="developer", body="dev")
    wf = _workflow()
    phase_files = [Path("/task/00-kickoff.md")]
    prompt = build_session_prompt(task, role, wf, Path("/toolbox"), None, phase_files)
    assert "00-kickoff.md" in prompt


def test_build_session_prompt_with_worksite_memory_preview():
    from simpleharness.core import Role

    task = _task()
    role = Role(name="developer", body="dev")
    wf = _workflow()
    preview = "line one\nline two\nline three"
    prompt = build_session_prompt(
        task, role, wf, Path("/toolbox"), None, [], worksite_memory_preview=preview
    )
    assert "Cross-session memory" in prompt
    assert "line one" in prompt
    assert "line three" in prompt


def test_build_session_prompt_worksite_memory_preview_none():
    from simpleharness.core import Role

    task = _task()
    role = Role(name="developer", body="dev")
    wf = _workflow()
    prompt = build_session_prompt(
        task, role, wf, Path("/toolbox"), None, [], worksite_memory_preview=None
    )
    assert "Cross-session memory" not in prompt


def test_build_session_prompt_worksite_memory_preview_empty():
    from simpleharness.core import Role

    task = _task()
    role = Role(name="developer", body="dev")
    wf = _workflow()
    prompt = build_session_prompt(
        task, role, wf, Path("/toolbox"), None, [], worksite_memory_preview=""
    )
    assert "Cross-session memory" not in prompt


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


def test_build_claude_cmd_approver_mode_no_settings():
    from simpleharness.core import Role

    role = Role(name="developer", body="dev")
    cfg = Config(permissions=Permissions(mode="approver"))
    cmd = build_claude_cmd(
        Path("/task/.session_prompt.md"),
        role,
        Path("/toolbox"),
        "sess-789",
        cfg,
        approver_settings_path=None,
    )
    assert "acceptEdits" in cmd
    assert "--settings" not in cmd


def test_build_claude_cmd_approver_mode_with_settings():
    from simpleharness.core import Role

    role = Role(name="developer", body="dev")
    cfg = Config(permissions=Permissions(mode="approver"))
    settings_path = Path("/tmp/approver_settings.json")
    cmd = build_claude_cmd(
        Path("/task/.session_prompt.md"),
        role,
        Path("/toolbox"),
        "sess-789",
        cfg,
        approver_settings_path=settings_path,
    )
    assert "acceptEdits" in cmd
    assert "--settings" in cmd
    assert str(settings_path) in cmd


# ── _format_tool_call: Glob/Grep ──────────────────────────────────────────────


def test_format_tool_call_glob_with_path():
    result = _format_tool_call("Glob", {"pattern": "**/*.py", "path": "/src"})
    assert "**/*.py" in result
    assert "/src" in result


def test_format_tool_call_glob_no_path():
    result = _format_tool_call("Glob", {"pattern": "*.md"})
    assert result == "*.md"


def test_format_tool_call_grep():
    result = _format_tool_call("Grep", {"pattern": "def foo", "path": "/src"})
    assert "def foo" in result
    assert "/src" in result


# ── parse_frontmatter: non-dict YAML ─────────────────────────────────────────


def test_parse_frontmatter_non_dict_yaml():
    # YAML that parses to a list, not a dict
    text = "---\n- item1\n- item2\n---\nbody"
    with pytest.raises(ValueError, match="frontmatter must be a mapping"):
        parse_frontmatter(text)


# ── toolbox_root ──────────────────────────────────────────────────────────────


def test_toolbox_root_returns_path():
    from simpleharness.core import toolbox_root

    result = toolbox_root()
    assert isinstance(result, Path)
    assert result.is_absolute()


# ── TaskSpec dataclasses ─────────────────────────────────────────────────────


def test_deliverable_is_frozen():
    d = Deliverable(path="out.md", description="report")
    with pytest.raises(dataclasses.FrozenInstanceError):
        cast(Any, d).path = "other.md"


def test_task_spec_is_frozen():
    ts = TaskSpec(title="t", workflow="universal")
    with pytest.raises(dataclasses.FrozenInstanceError):
        cast(Any, ts).title = "other"


def test_downstream_action_is_frozen():
    da = DownstreamAction(
        task_slug="002-foo",
        action="leave_active",
        upstream_deliverables=(),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        cast(Any, da).action = "block_for_rebrief"


def test_task_spec_defaults():
    ts = TaskSpec(title="t", workflow="w")
    assert ts.depends_on == ()
    assert ts.deliverables == ()
    assert ts.refine_on_deps_complete is False
    assert ts.references == ()


# ── parse_task_spec ──────────────────────────────────────────────────────────


def test_parse_task_spec_full():
    fm = {
        "title": "Add feature X",
        "workflow": "feature-build",
        "depends_on": ["001-refactor"],
        "deliverables": [
            {"path": "docs/report.md", "description": "Decision report"},
        ],
        "refine_on_deps_complete": True,
        "references": ["README.md", "docs/arch.md"],
    }
    spec = parse_task_spec(fm)
    assert spec.title == "Add feature X"
    assert spec.workflow == "feature-build"
    assert spec.depends_on == ("001-refactor",)
    assert spec.deliverables == (Deliverable("docs/report.md", "Decision report"),)
    assert spec.refine_on_deps_complete is True
    assert spec.references == ("README.md", "docs/arch.md")


def test_parse_task_spec_minimal():
    fm = {"title": "Fix bug", "workflow": "universal"}
    spec = parse_task_spec(fm)
    assert spec.title == "Fix bug"
    assert spec.workflow == "universal"
    assert spec.depends_on == ()
    assert spec.deliverables == ()
    assert spec.refine_on_deps_complete is False
    assert spec.references == ()


def test_parse_task_spec_empty_frontmatter():
    spec = parse_task_spec({})
    assert spec.title == ""
    assert spec.workflow == ""
    assert spec.depends_on == ()


def test_parse_task_spec_deliverable_as_string():
    """Deliverable can be a plain string (path only, no description)."""
    fm = {
        "title": "t",
        "workflow": "w",
        "deliverables": ["out.md"],
    }
    spec = parse_task_spec(fm)
    assert spec.deliverables == (Deliverable("out.md", ""),)


def test_parse_task_spec_none_fields_treated_as_empty():
    fm = {
        "title": "t",
        "workflow": "w",
        "depends_on": None,
        "deliverables": None,
        "references": None,
    }
    spec = parse_task_spec(fm)
    assert spec.depends_on == ()
    assert spec.deliverables == ()
    assert spec.references == ()


# ── deps_satisfied ───────────────────────────────────────────────────────────


def test_deps_satisfied_no_deps():
    spec = TaskSpec(title="t", workflow="w", depends_on=())
    assert deps_satisfied(spec, {}) is True


def test_deps_satisfied_all_done():
    spec = TaskSpec(title="t", workflow="w", depends_on=("001-a", "002-b"))
    states = {"001-a": "done", "002-b": "done", "003-c": "active"}
    assert deps_satisfied(spec, states) is True


def test_deps_satisfied_one_not_done():
    spec = TaskSpec(title="t", workflow="w", depends_on=("001-a", "002-b"))
    states = {"001-a": "done", "002-b": "active"}
    assert deps_satisfied(spec, states) is False


def test_deps_satisfied_missing_slug():
    spec = TaskSpec(title="t", workflow="w", depends_on=("001-a",))
    assert deps_satisfied(spec, {}) is False


# ── plan_downstream_transitions ──────────────────────────────────────────────


def test_plan_downstream_no_dependents():
    specs = {
        "001-a": TaskSpec(title="a", workflow="w"),
        "002-b": TaskSpec(title="b", workflow="w"),
    }
    result = plan_downstream_transitions("001-a", specs)
    assert result == ()


def test_plan_downstream_refine_true():
    specs = {
        "001-a": TaskSpec(
            title="a",
            workflow="w",
            deliverables=(Deliverable("out.md", "report"),),
        ),
        "002-b": TaskSpec(
            title="b",
            workflow="w",
            depends_on=("001-a",),
            refine_on_deps_complete=True,
        ),
    }
    result = plan_downstream_transitions("001-a", specs)
    assert len(result) == 1
    assert result[0].task_slug == "002-b"
    assert result[0].action == "leave_active"
    assert result[0].upstream_deliverables == (Deliverable("out.md", "report"),)


def test_plan_downstream_refine_false():
    specs = {
        "001-a": TaskSpec(title="a", workflow="w"),
        "002-b": TaskSpec(
            title="b",
            workflow="w",
            depends_on=("001-a",),
            refine_on_deps_complete=False,
        ),
    }
    result = plan_downstream_transitions("001-a", specs)
    assert len(result) == 1
    assert result[0].action == "block_for_rebrief"


def test_plan_downstream_multiple_dependents():
    specs = {
        "001-a": TaskSpec(title="a", workflow="w"),
        "002-b": TaskSpec(title="b", workflow="w", depends_on=("001-a",)),
        "003-c": TaskSpec(
            title="c",
            workflow="w",
            depends_on=("001-a",),
            refine_on_deps_complete=True,
        ),
    }
    result = plan_downstream_transitions("001-a", specs)
    assert len(result) == 2
    slugs = {r.task_slug for r in result}
    assert slugs == {"002-b", "003-c"}


# ── check_deliverables ──────────────────────────────────────────────────────


def test_check_deliverables_all_present():
    spec = TaskSpec(
        title="t",
        workflow="w",
        deliverables=(Deliverable("a.md", ""), Deliverable("b.md", "")),
    )
    missing = check_deliverables(spec, frozenset({"a.md", "b.md", "c.md"}))
    assert missing == ()


def test_check_deliverables_some_missing():
    spec = TaskSpec(
        title="t",
        workflow="w",
        deliverables=(Deliverable("a.md", ""), Deliverable("b.md", "")),
    )
    missing = check_deliverables(spec, frozenset({"a.md"}))
    assert missing == ("b.md",)


def test_check_deliverables_no_deliverables():
    spec = TaskSpec(title="t", workflow="w")
    missing = check_deliverables(spec, frozenset())
    assert missing == ()


def test_check_deliverables_min_lines_satisfied():
    spec = TaskSpec(
        title="t",
        workflow="w",
        deliverables=(Deliverable("a.md", "", min_lines=5),),
    )
    missing = check_deliverables(spec, frozenset({"a.md"}), {"a.md": 10})
    assert missing == ()


def test_check_deliverables_min_lines_not_met():
    spec = TaskSpec(
        title="t",
        workflow="w",
        deliverables=(Deliverable("a.md", "", min_lines=50),),
    )
    missing = check_deliverables(spec, frozenset({"a.md"}), {"a.md": 10})
    assert missing == ("a.md",)


def test_check_deliverables_min_lines_no_count_provided():
    spec = TaskSpec(
        title="t",
        workflow="w",
        deliverables=(Deliverable("a.md", "", min_lines=5),),
    )
    # File exists but no line count provided — treated as 0 lines, fails check
    missing = check_deliverables(spec, frozenset({"a.md"}))
    assert missing == ("a.md",)


def test_deliverable_min_lines_default_none():
    d = Deliverable("x.md", "doc")
    assert d.min_lines is None


def test_parse_task_spec_deliverable_min_lines():
    fm = {
        "title": "t",
        "workflow": "w",
        "deliverables": [{"path": "out.md", "description": "report", "min_lines": 100}],
    }
    spec = parse_task_spec(fm)
    assert spec.deliverables[0].min_lines == 100


def test_parse_task_spec_deliverable_no_min_lines():
    fm = {
        "title": "t",
        "workflow": "w",
        "deliverables": [{"path": "out.md", "description": "report"}],
    }
    spec = parse_task_spec(fm)
    assert spec.deliverables[0].min_lines is None


# ── pick_next_task with deps ─────────────────────────────────────────────────


def test_pick_next_task_skips_unmet_deps():
    """Task with unmet deps is skipped even if it's the lowest slug."""
    spec_with_dep = TaskSpec(title="b", workflow="w", depends_on=("000-prereq",))
    t1 = _task(slug="001-blocked", spec=spec_with_dep)
    t2 = _task(slug="002-free")
    result = pick_next_task([t1, t2], frozenset(), _FAR_PAST)
    assert result is not None
    assert result.slug == "002-free"


def test_pick_next_task_allows_met_deps():
    """Task with all deps done is a valid candidate."""
    spec_with_dep = TaskSpec(title="b", workflow="w", depends_on=("001-prereq",))
    t1 = _task(slug="001-prereq", state=_state(slug="001-prereq", status="done"))
    t2 = _task(slug="002-next", spec=spec_with_dep)
    result = pick_next_task([t1, t2], frozenset(), _FAR_PAST)
    assert result is not None
    assert result.slug == "002-next"


def test_pick_next_task_no_spec_treated_as_no_deps():
    """Tasks without a spec (old-style) are always eligible."""
    t = _task(slug="001-old", spec=None)
    result = pick_next_task([t], frozenset(), _FAR_PAST)
    assert result is not None
    assert result.slug == "001-old"


def test_pick_next_task_returns_none_when_all_deps_unmet():
    spec = TaskSpec(title="a", workflow="w", depends_on=("000-missing",))
    t = _task(slug="001-waiting", spec=spec)
    result = pick_next_task([t], frozenset(), _FAR_PAST)
    assert result is None


# ── plan_tick waiting_on_deps ────────────────────────────────────────────────


def test_plan_tick_waiting_on_deps():
    """Active task with unmet deps returns waiting_on_deps, not no_active."""
    spec = TaskSpec(title="a", workflow="w", depends_on=("000-prereq",))
    t = _task(slug="001-waiting", spec=spec)
    wf = {"w": _workflow(name="w")}
    plan = plan_tick((t,), wf, frozenset(), _config(), _FAR_PAST)
    assert plan.kind == "waiting_on_deps"


def test_plan_tick_no_active_when_truly_none():
    """Only done/blocked tasks → no_active (not waiting_on_deps)."""
    t = _task(slug="001-done", state=_state(slug="001-done", status="done"))
    wf = {"w": _workflow(name="w")}
    plan = plan_tick((t,), wf, frozenset(), _config(), _FAR_PAST)
    assert plan.kind == "no_active"


# ── build_refinement_text ────────────────────────────────────────────────────


def test_build_refinement_text_with_deliverables():
    deliverables = (
        Deliverable(path="output/report.md", description="Final report"),
        Deliverable(path="output/data.json", description="Raw data"),
    )
    text = build_refinement_text("000-upstream", deliverables)
    assert "000-upstream" in text
    assert "`output/report.md`: Final report" in text
    assert "`output/data.json`: Raw data" in text
    assert "NEEDS_REFINEMENT" not in text  # file name not baked in
    assert "# Refinement available" in text


def test_build_refinement_text_no_deliverables():
    text = build_refinement_text("000-upstream", ())
    assert "(none declared)" in text
    assert "000-upstream" in text


# ── build_rebrief_text ───────────────────────────────────────────────────────


def test_build_rebrief_text_with_deliverables():
    deliverables = (Deliverable(path="output/spec.md", description="Updated spec"),)
    text = build_rebrief_text("000-upstream", "002-downstream", deliverables)
    assert "000-upstream" in text
    assert "`output/spec.md`: Updated spec" in text
    assert "simpleharness unblock 002-downstream" in text
    assert "# Rebrief needed" in text


def test_build_rebrief_text_no_deliverables():
    text = build_rebrief_text("000-upstream", "002-downstream", ())
    assert "(none declared)" in text
    assert "simpleharness unblock 002-downstream" in text


def test_state_cost_roundtrips_through_io(tmp_path):
    from simpleharness.io import read_state, write_state

    state = _state(total_cost_usd=2.75)
    path = tmp_path / "STATE.md"
    write_state(path, state)
    loaded = read_state(path)
    assert loaded.total_cost_usd == 2.75


# ── pause_file_path ───────────────────────────────────────────────────────────


def test_pause_file_path():
    p = pause_file_path(Path("/worksite"))
    assert p == Path("/worksite/simpleharness/.PAUSE")


def test_format_task_dashboard_active():
    state = _state(phase="plan", total_sessions=3, session_cap=20, total_cost_usd=1.50)
    result = format_task_dashboard(state, ("kickoff", "brainstorm", "plan", "develop", "review"))
    assert "[PLAN]" in result["phase_progress"]
    assert "kickoff" in result["phase_progress"]
    assert result["sessions"] == "3/20"
    assert result["cost"] == "$1.50"


def test_format_task_dashboard_no_cost():
    state = _state(total_cost_usd=0.0)
    result = format_task_dashboard(state, ("kickoff",))
    assert result["cost"] == "\u2014"


def test_format_task_dashboard_empty_phases():
    state = _state(phase="custom")
    result = format_task_dashboard(state, ())
    assert result["phase_progress"] == "custom"


# ── State retry fields ────────────────────────────────────────────────────────


def test_state_retry_fields_default():
    s = _state()
    assert s.retry_count == 0
    assert s.retry_after is None


def test_state_retry_fields_set():
    s = _state(retry_count=3, retry_after="2026-04-09T16:00:00Z")
    assert s.retry_count == 3
    assert s.retry_after == "2026-04-09T16:00:00Z"


def test_state_retry_fields_roundtrip(tmp_path):
    """read_state and write_state preserve retry fields."""
    from simpleharness.io import read_state, write_state

    path = tmp_path / "STATE.md"
    original = _state(retry_count=2, retry_after="2026-04-09T16:00:00Z")
    write_state(path, original)
    restored = read_state(path)
    assert restored.retry_count == 2
    assert restored.retry_after == "2026-04-09T16:00:00Z"


def test_state_retry_fields_roundtrip_defaults(tmp_path):
    """Old STATE.md files without retry fields parse with defaults."""
    from simpleharness.io import read_state, write_state

    path = tmp_path / "STATE.md"
    original = _state()  # retry_count=0, retry_after=None
    write_state(path, original)
    restored = read_state(path)
    assert restored.retry_count == 0
    assert restored.retry_after is None


# ── classify_cli_error ────────────────────────────────────────────────────────


def test_classify_usage_limit_with_reset():
    r = classify_cli_error(1, "Usage limit reached. Reset at 2026-04-09T17:00:00Z.")
    assert r.outcome == "usage_limit"
    assert r.retry_after_iso == "2026-04-09T17:00:00Z"


def test_classify_transient_overloaded():
    r = classify_cli_error(1, "API is overloaded, please retry later")
    assert r.outcome == "transient"


def test_classify_transient_529():
    r = classify_cli_error(1, "HTTP 529 error from upstream")
    assert r.outcome == "transient"


def test_classify_transient_503():
    r = classify_cli_error(1, "503 Service Unavailable")
    assert r.outcome == "transient"


def test_classify_transient_rate_limit():
    r = classify_cli_error(1, "Rate limit exceeded")
    assert r.outcome == "transient"


def test_classify_transient_econnreset():
    r = classify_cli_error(1, "ECONNRESET: connection reset by peer")
    assert r.outcome == "transient"


def test_classify_transient_etimedout():
    r = classify_cli_error(1, "ETIMEDOUT: connection timed out")
    assert r.outcome == "transient"


def test_classify_transient_dns():
    r = classify_cli_error(1, "DNS resolution failed for api.anthropic.com")
    assert r.outcome == "transient"


def test_classify_transient_timeout():
    r = classify_cli_error(1, "Request timeout after 30s")
    assert r.outcome == "transient"


def test_classify_fatal_401():
    r = classify_cli_error(1, "401 Unauthorized")
    assert r.outcome == "fatal"
    assert "auth_expired" in r.reason


def test_classify_fatal_invalid_api_key():
    r = classify_cli_error(1, "Invalid API key provided")
    assert r.outcome == "fatal"
    assert "auth_expired" in r.reason


def test_classify_fatal_not_authenticated():
    r = classify_cli_error(1, "Not authenticated — please run claude login")
    assert r.outcome == "fatal"
    assert "auth_expired" in r.reason


def test_classify_fatal_token_expired():
    r = classify_cli_error(1, "Token expired, re-authenticate")
    assert r.outcome == "fatal"
    assert "auth_expired" in r.reason


def test_classify_fatal_unknown():
    r = classify_cli_error(1, "Something completely unexpected happened")
    assert r.outcome == "fatal"
    assert "unexpected" in r.reason.lower()


def test_classify_fatal_empty_error_text():
    r = classify_cli_error(42, "")
    assert r.outcome == "fatal"
    assert "42" in r.reason


def test_classify_usage_limit_priority_over_transient():
    """Usage limit with reset time should match usage_limit, not transient."""
    r = classify_cli_error(1, "Rate limit: usage limit reached. Reset at 2026-04-09T18:00:00Z")
    assert r.outcome == "usage_limit"


# ── compute_backoff_delay ─────────────────────────────────────────────────────


def test_backoff_delay_first_retry():
    assert compute_backoff_delay(0) == 30


def test_backoff_delay_second_retry():
    assert compute_backoff_delay(1) == 60


def test_backoff_delay_third_retry():
    assert compute_backoff_delay(2) == 120


def test_backoff_delay_fourth_retry():
    assert compute_backoff_delay(3) == 240


def test_backoff_delay_fifth_retry():
    assert compute_backoff_delay(4) == 300


def test_backoff_delay_exhausted():
    assert compute_backoff_delay(5) is None


def test_backoff_delay_way_past_exhausted():
    assert compute_backoff_delay(99) is None


def test_backoff_delay_custom_schedule():
    assert compute_backoff_delay(0, schedule=(10, 20)) == 10
    assert compute_backoff_delay(1, schedule=(10, 20)) == 20
    assert compute_backoff_delay(2, schedule=(10, 20)) is None


def test_default_backoff_schedule_values():
    assert DEFAULT_BACKOFF_SCHEDULE == (30, 60, 120, 240, 300)


# ── compute_post_session_state retry logic ────────────────────────────────────

_RETRY_NOW = datetime(2026, 4, 9, 15, 0, 0, tzinfo=UTC)


def test_post_session_clears_retry_on_success():
    state = _state(retry_count=3, retry_after="2026-04-09T14:00:00Z")
    session = _session(completed=True, exit_code=0)
    new = compute_post_session_state(
        state,
        "dev",
        session,
        None,
        0,
        "pre",
        "post",
        _config(),
        _RETRY_NOW,
        classify_result=None,
    )
    assert new.retry_count == 0
    assert new.retry_after is None


def test_post_session_transient_bumps_retry():
    state = _state(retry_count=0)
    session = _session(completed=False, exit_code=1)
    cr = ClassifyResult("transient", "matched transient pattern: overloaded")
    new = compute_post_session_state(
        state,
        "dev",
        session,
        None,
        0,
        "pre",
        "post",
        _config(),
        _RETRY_NOW,
        classify_result=cr,
    )
    assert new.retry_count == 1
    assert new.retry_after is not None
    assert new.status == "active"


def test_post_session_transient_sets_correct_backoff():
    state = _state(retry_count=2)  # 3rd retry → 120s delay
    session = _session(completed=False, exit_code=1)
    cr = ClassifyResult("transient", "503")
    new = compute_post_session_state(
        state,
        "dev",
        session,
        None,
        0,
        "pre",
        "post",
        _config(),
        _RETRY_NOW,
        classify_result=cr,
    )
    assert new.retry_count == 3
    expected_after = (_RETRY_NOW + timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert new.retry_after == expected_after


def test_post_session_transient_exhausted_blocks():
    state = _state(retry_count=5)  # 6th retry → exhausted (schedule has 5 entries)
    session = _session(completed=False, exit_code=1)
    cr = ClassifyResult("transient", "overloaded")
    new = compute_post_session_state(
        state,
        "dev",
        session,
        None,
        0,
        "pre",
        "post",
        _config(),
        _RETRY_NOW,
        classify_result=cr,
    )
    assert new.status == "blocked"
    assert "retries exhausted" in (new.blocked_reason or "")
    assert new.retry_count == 6
    assert new.retry_after is None


def test_post_session_usage_limit_parks_task():
    state = _state(retry_count=0)
    session = _session(completed=False, exit_code=1)
    cr = ClassifyResult("usage_limit", "usage limit hit", retry_after_iso="2026-04-09T17:00:00Z")
    new = compute_post_session_state(
        state,
        "dev",
        session,
        None,
        0,
        "pre",
        "post",
        _config(),
        _RETRY_NOW,
        classify_result=cr,
    )
    assert new.retry_after == "2026-04-09T17:00:00Z"
    assert new.retry_count == 0  # not bumped for usage limits
    assert new.status == "active"


def test_post_session_fatal_blocks_task():
    state = _state(retry_count=2)
    session = _session(completed=False, exit_code=1)
    cr = ClassifyResult("fatal", "auth_expired — run claude login in container")
    new = compute_post_session_state(
        state,
        "dev",
        session,
        None,
        0,
        "pre",
        "post",
        _config(),
        _RETRY_NOW,
        classify_result=cr,
    )
    assert new.status == "blocked"
    assert "auth_expired" in (new.blocked_reason or "")
    assert new.retry_count == 0
    assert new.retry_after is None


def test_post_session_no_classify_result_no_change():
    """When classify_result is None and session didn't complete, retry fields unchanged."""
    state = _state(retry_count=0)
    session = _session(completed=False, exit_code=1)
    new = compute_post_session_state(
        state,
        "dev",
        session,
        None,
        0,
        "pre",
        "post",
        _config(),
        _RETRY_NOW,
        classify_result=None,
    )
    assert new.retry_count == 0
    assert new.retry_after is None


# ── pick_next_task backoff filtering ──────────────────────────────────────────

_BACKOFF_NOW = datetime(2026, 4, 9, 15, 0, 0, tzinfo=UTC)


def test_pick_next_task_skips_backoff():
    """Task in backoff (retry_after in the future) is skipped."""
    t = _task(state=_state(retry_after="2026-04-09T16:00:00Z"))
    result = pick_next_task((t,), frozenset(), _BACKOFF_NOW)
    assert result is None


def test_pick_next_task_picks_past_backoff():
    """Task whose retry_after is in the past is eligible."""
    t = _task(state=_state(retry_after="2026-04-09T14:00:00Z"))
    result = pick_next_task((t,), frozenset(), _BACKOFF_NOW)
    assert result is not None
    assert result.slug == "001-test"


def test_pick_next_task_correction_overrides_backoff():
    """Task with CORRECTION.md bypasses backoff filter."""
    t = _task(slug="001-test", state=_state(slug="001-test", retry_after="2026-04-09T16:00:00Z"))
    result = pick_next_task((t,), frozenset({"001-test"}), _BACKOFF_NOW)
    assert result is not None
    assert result.slug == "001-test"


def test_pick_next_task_no_retry_after_is_eligible():
    """Task with retry_after=None is always eligible."""
    t = _task(state=_state(retry_after=None))
    result = pick_next_task((t,), frozenset(), _BACKOFF_NOW)
    assert result is not None


# ── plan_tick all_backoff ─────────────────────────────────────────────────────


def test_plan_tick_all_backoff():
    t = _task(state=_state(retry_after="2026-04-09T16:00:00Z"))
    plan = plan_tick((t,), {"default": _workflow()}, frozenset(), _config(), _BACKOFF_NOW)
    assert plan.kind == "all_backoff"


def test_plan_tick_run_with_now():
    """plan_tick still produces 'run' when task is not in backoff."""
    t = _task(state=_state(retry_after=None))
    plan = plan_tick((t,), {"default": _workflow()}, frozenset(), _config(), _BACKOFF_NOW)
    assert plan.kind == "run"
