"""Tests for parse_skill_list and merge_skill_lists in simpleharness.core."""

from __future__ import annotations

import pytest

from simpleharness.core import Skill, SkillList, merge_skill_lists, parse_skill_list

# ── parse_skill_list ──────────────────────────────────────────────────────────


def test_parse_skill_list_none_returns_empty() -> None:
    result = parse_skill_list(None)
    assert result == SkillList()


def test_parse_skill_list_empty_dict_returns_empty() -> None:
    result = parse_skill_list({})
    assert result == SkillList()


def test_parse_skill_list_full() -> None:
    raw = {
        "available": [
            {"name": "humanizer", "hint": "strip AI-voice"},
            {"name": "writing-skills", "hint": "for authoring"},
        ],
        "must_use": ["humanizer"],
        "exclude_default_must_use": ["updating-memory"],
    }
    result = parse_skill_list(raw)
    assert result.available == (
        Skill(name="humanizer", hint="strip AI-voice"),
        Skill(name="writing-skills", hint="for authoring"),
    )
    assert result.must_use == ("humanizer",)
    assert result.exclude_default_must_use == ("updating-memory",)


def test_parse_skill_list_bare_string_entries() -> None:
    raw = {"available": ["humanizer", "writing-skills"]}
    result = parse_skill_list(raw)
    assert result.available == (
        Skill(name="humanizer", hint=""),
        Skill(name="writing-skills", hint=""),
    )


def test_parse_skill_list_must_use_not_a_list_raises() -> None:
    raw = {"must_use": "not-a-list"}
    with pytest.raises(ValueError, match="must_use"):
        parse_skill_list(raw)


def test_parse_skill_list_non_dict_root_raises() -> None:
    with pytest.raises(ValueError, match="must be a mapping"):
        parse_skill_list("bad-value")


def test_parse_skill_list_available_entry_missing_name_raises() -> None:
    raw = {"available": [{"hint": "no name here"}]}
    with pytest.raises(ValueError, match="name"):
        parse_skill_list(raw)


def test_parse_skill_list_available_not_a_list_raises() -> None:
    raw = {"available": "not-a-list"}
    with pytest.raises(ValueError, match="available"):
        parse_skill_list(raw)


# ── merge_skill_lists ─────────────────────────────────────────────────────────


def test_merge_skill_lists_defaults_merge_into_role() -> None:
    default = SkillList(
        available=(Skill("common", "default hint"),),
        must_use=("common",),
    )
    role = SkillList(
        available=(Skill("role-only", "role hint"),),
        must_use=("role-only",),
    )
    result = merge_skill_lists(role, default)
    assert Skill("common", "default hint") in result.available
    assert Skill("role-only", "role hint") in result.available
    assert "common" in result.must_use
    assert "role-only" in result.must_use


def test_merge_skill_lists_role_hints_win_on_collision() -> None:
    default = SkillList(available=(Skill("shared", "default hint"),))
    role = SkillList(available=(Skill("shared", "role hint"),))
    result = merge_skill_lists(role, default)
    assert result.available == (Skill("shared", "role hint"),)


def test_merge_skill_lists_exclude_removes_defaults() -> None:
    default = SkillList(must_use=("updating-memory", "humanizer"))
    role = SkillList(
        must_use=("role-skill",),
        exclude_default_must_use=("updating-memory",),
    )
    result = merge_skill_lists(role, default)
    assert "updating-memory" not in result.must_use
    assert "humanizer" in result.must_use
    assert "role-skill" in result.must_use


def test_merge_skill_lists_empty_defaults_role_unchanged() -> None:
    role = SkillList(
        available=(Skill("x", "hint"),),
        must_use=("x",),
    )
    result = merge_skill_lists(role, SkillList())
    assert result.available == (Skill("x", "hint"),)
    assert result.must_use == ("x",)


def test_merge_skill_lists_order_defaults_first() -> None:
    default = SkillList(must_use=("alpha", "beta"))
    role = SkillList(must_use=("gamma",))
    result = merge_skill_lists(role, default)
    assert list(result.must_use) == ["alpha", "beta", "gamma"]


def test_merge_skill_lists_exclude_default_must_use_carried_through() -> None:
    role = SkillList(exclude_default_must_use=("skip-this",))
    result = merge_skill_lists(role, SkillList())
    assert result.exclude_default_must_use == ("skip-this",)
