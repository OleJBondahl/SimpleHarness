"""Hypothesis property-based tests for pure functions in core.py and approver_core.py."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from simpleharness.approver_core import (
    Verdict,
    command_signature,
    parse_verdict,
    unwrap_wrappers,
)
from simpleharness.core import (
    DEFAULT_BACKOFF_SCHEDULE,
    ClassifyResult,
    Config,
    Deliverable,
    LoopConfig,
    LoopState,
    Role,
    Skill,
    SkillList,
    State,
    Task,
    TaskSpec,
    _parse_retry_after,
    _slugify,
    apply_critique_verdict,
    apply_e2e_verdict,
    apply_review_verdict,
    build_session_env,
    check_deliverables,
    classify_cli_error,
    compute_backoff_delay,
    deps_satisfied,
    merge_skill_lists,
    parse_frontmatter,
    pick_next_task,
    resolve_loop_role,
)

# ── parse_frontmatter ─────────────────────────────────────────────────────────


@given(text=st.text())
def test_parse_frontmatter_always_returns_tuple(text: str) -> None:
    """parse_frontmatter always returns a (dict, str) tuple or raises ValueError."""
    try:
        meta, body = parse_frontmatter(text)
    except ValueError:
        return  # allowed for malformed frontmatter
    assert isinstance(meta, dict)
    assert isinstance(body, str)


@given(meta_items=st.dictionaries(st.from_regex(r"[a-z_]+", fullmatch=True), st.integers()))
def test_parse_frontmatter_round_trip(meta_items: dict) -> None:
    """A doc with valid YAML frontmatter round-trips correctly."""
    import yaml

    yaml_str = yaml.dump(meta_items).strip()
    text = f"---\n{yaml_str}\n---\nbody text"
    meta, body = parse_frontmatter(text)
    assert meta == meta_items
    assert body == "body text"


@given(body=st.text(alphabet=st.characters(blacklist_characters="-")))
def test_parse_frontmatter_no_frontmatter_returns_empty_dict(body: str) -> None:
    """Text without frontmatter returns ({}, original_text)."""
    # Strip leading '---' to avoid accidental frontmatter match
    safe_body = body.lstrip("-")
    meta, returned_body = parse_frontmatter(safe_body)
    assert meta == {}
    assert returned_body == safe_body


# ── _slugify ──────────────────────────────────────────────────────────────────


@given(text=st.text())
def test_slugify_always_lowercase(text: str) -> None:
    result = _slugify(text)
    assert result == result.lower()


@given(text=st.text())
def test_slugify_no_spaces(text: str) -> None:
    result = _slugify(text)
    assert " " not in result


@given(text=st.text())
def test_slugify_idempotent(text: str) -> None:
    once = _slugify(text)
    twice = _slugify(once)
    assert once == twice


@given(text=st.text())
def test_slugify_max_length_60_and_nonempty(text: str) -> None:
    result = _slugify(text)
    assert 1 <= len(result) <= 60


# ── compute_backoff_delay ─────────────────────────────────────────────────────


@given(retry_count=st.integers(min_value=0, max_value=len(DEFAULT_BACKOFF_SCHEDULE) - 1))
def test_compute_backoff_delay_non_negative_within_schedule(retry_count: int) -> None:
    result = compute_backoff_delay(retry_count)
    assert result is not None
    assert result >= 0


@given(retry_count=st.integers(min_value=len(DEFAULT_BACKOFF_SCHEDULE)))
def test_compute_backoff_delay_exhausted_returns_none(retry_count: int) -> None:
    assert compute_backoff_delay(retry_count) is None


@given(
    a=st.integers(min_value=0, max_value=len(DEFAULT_BACKOFF_SCHEDULE) - 2),
    b=st.integers(min_value=1, max_value=len(DEFAULT_BACKOFF_SCHEDULE) - 1),
)
def test_compute_backoff_delay_non_decreasing(a: int, b: int) -> None:
    """Later retries have equal or longer delays."""
    if a >= b:
        a, b = b, a
    da = compute_backoff_delay(a)
    db = compute_backoff_delay(b)
    if da is not None and db is not None:
        assert da <= db


# ── _parse_retry_after ────────────────────────────────────────────────────────


@given(
    year=st.integers(2020, 2030),
    month=st.integers(1, 12),
    day=st.integers(1, 28),
    hour=st.integers(0, 23),
    minute=st.integers(0, 59),
    second=st.integers(0, 59),
)
def test_parse_retry_after_valid_iso_parses(
    year: int, month: int, day: int, hour: int, minute: int, second: int
) -> None:
    iso = f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}Z"
    result = _parse_retry_after(iso)
    assert isinstance(result, datetime)


@given(junk=st.text(alphabet="xyz!@#", min_size=1))
def test_parse_retry_after_invalid_returns_none(junk: str) -> None:
    assert _parse_retry_after(junk) is None


# ── build_session_env ─────────────────────────────────────────────────────────

_skill_name_st = st.from_regex(r"[a-z][a-z0-9-]{0,19}", fullmatch=True)


@given(
    base_env=st.dictionaries(st.text(min_size=1, max_size=20), st.text(max_size=40), max_size=5),
    role_name=_skill_name_st,
)
@settings(max_examples=50)
def test_build_session_env_always_has_required_keys(base_env: dict, role_name: str) -> None:
    role = Role(name=role_name, body="")
    config = Config()
    result = build_session_env(base_env, role, (), config)
    assert isinstance(result, dict)
    for key in (
        "SIMPLEHARNESS_ROLE",
        "SIMPLEHARNESS_AVAILABLE_SKILLS",
        "SIMPLEHARNESS_MUST_USE_MAIN",
        "SIMPLEHARNESS_MUST_USE_SUB",
        "SIMPLEHARNESS_ENFORCEMENT",
    ):
        assert key in result, f"missing key: {key}"


@given(
    base_env=st.dictionaries(st.text(min_size=1, max_size=20), st.text(max_size=40), max_size=5),
    role_name=_skill_name_st,
)
@settings(max_examples=50)
def test_build_session_env_does_not_mutate_base(base_env: dict, role_name: str) -> None:
    original = dict(base_env)
    role = Role(name=role_name, body="")
    build_session_env(base_env, role, (), Config())
    assert base_env == original


@given(
    base_env=st.dictionaries(st.text(min_size=1, max_size=20), st.text(max_size=40), max_size=5),
    role_name=_skill_name_st,
)
@settings(max_examples=50)
def test_build_session_env_json_values_are_valid(base_env: dict, role_name: str) -> None:
    """SIMPLEHARNESS_* JSON vars must be valid JSON."""
    role = Role(name=role_name, body="")
    result = build_session_env(base_env, role, (), Config())
    for key in (
        "SIMPLEHARNESS_AVAILABLE_SKILLS",
        "SIMPLEHARNESS_MUST_USE_MAIN",
        "SIMPLEHARNESS_MUST_USE_SUB",
    ):
        json.loads(result[key])  # raises if invalid


# ── merge_skill_lists ─────────────────────────────────────────────────────────

_skill_st = st.builds(Skill, name=_skill_name_st, hint=st.text(max_size=30))
_skill_list_st = st.builds(
    SkillList,
    available=st.lists(_skill_st, max_size=5).map(tuple),
    must_use=st.lists(_skill_name_st, max_size=5).map(tuple),
    exclude_default_must_use=st.just(()),
)


@given(role_skills=_skill_list_st, default_skills=_skill_list_st)
def test_merge_skill_lists_result_contains_all_role_must_use(
    role_skills: SkillList, default_skills: SkillList
) -> None:
    merged = merge_skill_lists(role_skills, default_skills)
    for name in role_skills.must_use:
        assert name in merged.must_use


@given(role_skills=_skill_list_st, default_skills=_skill_list_st)
def test_merge_skill_lists_no_duplicates_in_must_use(
    role_skills: SkillList, default_skills: SkillList
) -> None:
    merged = merge_skill_lists(role_skills, default_skills)
    assert len(merged.must_use) == len(set(merged.must_use))


# ── classify_cli_error ───────────────────────────────────────────────────────


@given(exit_code=st.integers(0, 255), error_text=st.text(max_size=200))
@settings(max_examples=100)
def test_classify_cli_error_always_returns_classify_result(exit_code: int, error_text: str) -> None:
    result = classify_cli_error(exit_code, error_text)
    assert isinstance(result, ClassifyResult)
    assert result.outcome in ("usage_limit", "transient", "fatal")
    assert isinstance(result.reason, str)
    assert len(result.reason) > 0


@given(
    year=st.integers(2024, 2030),
    month=st.integers(1, 12),
    day=st.integers(1, 28),
    hour=st.integers(0, 23),
    minute=st.integers(0, 59),
)
def test_classify_cli_error_usage_limit_detected(
    year: int, month: int, day: int, hour: int, minute: int
) -> None:
    ts = f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:00Z"
    error = f"Your account has reached its usage limit. Usage resets at {ts}."
    result = classify_cli_error(1, error)
    assert result.outcome == "usage_limit"
    assert result.retry_after_iso is not None


# ── deps_satisfied ───────────────────────────────────────────────────────────

_slug_st = st.from_regex(r"[a-z][a-z0-9-]{0,9}", fullmatch=True)


@given(
    dep_slugs=st.lists(_slug_st, min_size=1, max_size=5, unique=True).map(tuple),
)
def test_deps_satisfied_all_done_returns_true(dep_slugs: tuple[str, ...]) -> None:
    spec = TaskSpec(title="t", workflow="w", depends_on=dep_slugs)
    states = dict.fromkeys(dep_slugs, "done")
    assert deps_satisfied(spec, states) is True


@given(
    dep_slugs=st.lists(_slug_st, min_size=1, max_size=5, unique=True).map(tuple),
)
def test_deps_satisfied_any_active_returns_false(dep_slugs: tuple[str, ...]) -> None:
    spec = TaskSpec(title="t", workflow="w", depends_on=dep_slugs)
    states = dict.fromkeys(dep_slugs, "done")
    states[dep_slugs[0]] = "active"
    assert deps_satisfied(spec, states) is False


@given(dep_slugs=st.lists(_slug_st, min_size=1, max_size=5, unique=True).map(tuple))
def test_deps_satisfied_missing_slug_returns_false(dep_slugs: tuple[str, ...]) -> None:
    spec = TaskSpec(title="t", workflow="w", depends_on=dep_slugs)
    assert deps_satisfied(spec, {}) is False


# ── check_deliverables ───────────────────────────────────────────────────────


@given(
    paths=st.lists(
        st.from_regex(r"[a-z]{1,10}\.py", fullmatch=True), min_size=1, max_size=5, unique=True
    ),
)
def test_check_deliverables_all_present_returns_empty(paths: list[str]) -> None:
    deliverables = tuple(Deliverable(path=p) for p in paths)
    spec = TaskSpec(title="t", workflow="w", deliverables=deliverables)
    existing = frozenset(paths)
    assert check_deliverables(spec, existing) == ()


@given(
    paths=st.lists(
        st.from_regex(r"[a-z]{1,10}\.py", fullmatch=True), min_size=1, max_size=5, unique=True
    ),
)
def test_check_deliverables_none_present_returns_all(paths: list[str]) -> None:
    deliverables = tuple(Deliverable(path=p) for p in paths)
    spec = TaskSpec(title="t", workflow="w", deliverables=deliverables)
    missing = check_deliverables(spec, frozenset())
    assert set(missing) == set(paths)


@given(
    min_lines=st.integers(min_value=2, max_value=100),
    actual_lines=st.integers(min_value=0, max_value=1),
)
def test_check_deliverables_min_lines_not_met(min_lines: int, actual_lines: int) -> None:
    d = Deliverable(path="f.py", min_lines=min_lines)
    spec = TaskSpec(title="t", workflow="w", deliverables=(d,))
    missing = check_deliverables(spec, frozenset({"f.py"}), {"f.py": actual_lines})
    assert "f.py" in missing


# ── apply_review_verdict / apply_critique_verdict / apply_e2e_verdict ────────

_loop_config_st = st.builds(
    LoopConfig,
    roles=st.just(("builder", "reviewer", "critic")),
    max_cycles=st.integers(1, 10),
    max_critic_rounds=st.integers(1, 5),
)

_loop_state_st = st.builds(
    LoopState,
    current_step=st.integers(0, 4),
    total_steps=st.integers(1, 10),
    cycle=st.integers(0, 9),
    critic_rounds=st.integers(0, 4),
    inner_phase=st.sampled_from(("building", "reviewing", "critiquing", "e2e_testing")),
)


@given(ls=_loop_state_st, lc=_loop_config_st)
def test_apply_review_pass_moves_to_critiquing(ls: LoopState, lc: LoopConfig) -> None:
    result = apply_review_verdict(ls, lc, verdict="pass")
    assert result.inner_phase == "critiquing"


@given(ls=_loop_state_st, lc=_loop_config_st)
def test_apply_review_fail_stays_building_or_advances(ls: LoopState, lc: LoopConfig) -> None:
    result = apply_review_verdict(ls, lc, verdict="fail")
    assert result.inner_phase in ("building", "e2e_testing")


@given(ls=_loop_state_st, lc=_loop_config_st, verdict=st.sampled_from(("approved", "suggestions")))
def test_apply_critique_never_crashes(ls: LoopState, lc: LoopConfig, verdict: str) -> None:
    result = apply_critique_verdict(ls, lc, verdict=verdict)
    assert isinstance(result, LoopState)
    assert result.inner_phase in ("building", "e2e_testing")


@given(ls=_loop_state_st, lc=_loop_config_st, verdict=st.sampled_from(("pass", "fail")))
def test_apply_e2e_verdict_pass_is_done(ls: LoopState, lc: LoopConfig, verdict: str) -> None:
    result = apply_e2e_verdict(ls, lc, verdict=verdict)
    if verdict == "pass":
        assert result.inner_phase == "done"
    else:
        assert result.inner_phase == "building"


# ── resolve_loop_role ────────────────────────────────────────────────────────


@given(
    ls=_loop_state_st,
    lc=_loop_config_st,
)
def test_resolve_loop_role_returns_valid_role(ls: LoopState, lc: LoopConfig) -> None:
    role_name, new_state = resolve_loop_role(ls, lc)
    assert role_name in lc.roles
    assert isinstance(new_state, LoopState)
    assert new_state.last_inner_role == role_name


# ── pick_next_task ───────────────────────────────────────────────────────────

_now = datetime(2026, 1, 1)


def _mk_task(slug: str, status: str = "active") -> Task:
    folder = Path(f"/tmp/{slug}")
    state = State(
        task_slug=slug, workflow="default", worksite="/tmp", toolbox="/tmp/toolbox", status=status
    )
    return Task(
        slug=slug,
        folder=folder,
        task_md=folder / "TASK.md",
        state_path=folder / "STATE.md",
        state=state,
    )


@given(slugs=st.lists(_slug_st, min_size=1, max_size=5, unique=True))
def test_pick_next_task_returns_lowest_slug(slugs: list[str]) -> None:
    tasks = [_mk_task(s) for s in slugs]
    result = pick_next_task(tasks, frozenset(), _now)
    assert result is not None
    expected = sorted(slugs)[0]
    assert result.slug == expected


def test_pick_next_task_corrections_take_priority() -> None:
    tasks = [_mk_task("aaa"), _mk_task("zzz")]
    result = pick_next_task(tasks, frozenset({"zzz"}), _now)
    assert result is not None
    assert result.slug == "zzz"


def test_pick_next_task_skips_blocked() -> None:
    tasks = [_mk_task("a", status="blocked"), _mk_task("b")]
    result = pick_next_task(tasks, frozenset(), _now)
    assert result is not None
    assert result.slug == "b"


# ── unwrap_wrappers (approver_core) ─────────────────────────────────────────


@given(cmd=st.lists(st.text(min_size=1, max_size=20), min_size=1, max_size=10))
def test_unwrap_wrappers_result_is_sublist(cmd: list[str]) -> None:
    result = unwrap_wrappers(cmd)
    assert len(result) <= len(cmd)


def test_unwrap_wrappers_strips_sudo() -> None:
    assert unwrap_wrappers(["sudo", "ls", "-la"]) == ["ls", "-la"]


def test_unwrap_wrappers_strips_env() -> None:
    assert unwrap_wrappers(["env", "FOO=bar", "python"]) == ["python"]


@given(depth=st.integers(1, 8))
def test_unwrap_wrappers_bounded_recursion(depth: int) -> None:
    tokens = ["sudo"] * depth + ["ls"]
    result = unwrap_wrappers(tokens, max_depth=depth)
    assert result == ["ls"]


# ── command_signature (approver_core) ────────────────────────────────────────


@given(cmd=st.text(max_size=200))
def test_command_signature_never_empty(cmd: str) -> None:
    result = command_signature(cmd)
    assert isinstance(result, str)
    assert len(result) > 0


def test_command_signature_extracts_base_command() -> None:
    assert command_signature("sudo apt install git") == "apt"
    assert command_signature("ls -la") == "ls"


# ── parse_verdict (approver_core) ────────────────────────────────────────────


@given(text=st.text(max_size=500))
def test_parse_verdict_never_raises(text: str) -> None:
    result = parse_verdict(text)
    assert isinstance(result, Verdict)
    assert result.decision in ("allow", "deny")


@given(
    decision=st.sampled_from(("allow", "deny")),
    pattern=st.from_regex(r"[a-z]{1,20}", fullmatch=True),
    reason=st.from_regex(r"[a-z ]{1,40}", fullmatch=True),
)
def test_parse_verdict_valid_json_round_trips(decision: str, pattern: str, reason: str) -> None:
    assume(reason.strip())
    assume(decision == "deny" or pattern.strip())
    obj = {"decision": decision, "pattern": pattern, "reason": reason}
    text = f"```json\n{json.dumps(obj)}\n```"
    result = parse_verdict(text)
    assert result.decision == decision
