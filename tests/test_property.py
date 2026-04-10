"""Hypothesis property-based tests for pure functions in core.py."""

from __future__ import annotations

import json
from datetime import datetime

from hypothesis import given, settings
from hypothesis import strategies as st

from simpleharness.core import (
    DEFAULT_BACKOFF_SCHEDULE,
    Config,
    Role,
    Skill,
    SkillList,
    _parse_retry_after,
    _slugify,
    build_session_env,
    compute_backoff_delay,
    merge_skill_lists,
    parse_frontmatter,
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
