"""Tests for load_role, load_subagent, load_all_subagents in simpleharness.shell."""

from __future__ import annotations

from pathlib import Path

import pytest

from simpleharness.core import Skill, SkillList
from simpleharness.shell import load_all_subagents, load_role, load_subagent


def _write_md(path: Path, frontmatter: str, body: str = "Role body.") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}---\n{body}", encoding="utf-8")


# ── load_role ─────────────────────────────────────────────────────────────────


def test_load_role_with_skills_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("simpleharness.core._TOOLBOX_ROOT", tmp_path)
    _write_md(
        tmp_path / "roles" / "writer.md",
        (
            "name: writer\n"
            "description: Writes stuff.\n"
            "skills:\n"
            "  available:\n"
            "    - name: humanizer\n"
            "      hint: strip AI voice\n"
            "  must_use:\n"
            "    - humanizer\n"
        ),
    )
    role = load_role("writer")
    assert role.skills == SkillList(
        available=(Skill("humanizer", "strip AI voice"),),
        must_use=("humanizer",),
    )


def test_load_role_without_skills_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("simpleharness.core._TOOLBOX_ROOT", tmp_path)
    _write_md(
        tmp_path / "roles" / "plain.md",
        "name: plain\ndescription: Plain role.\n",
    )
    role = load_role("plain")
    assert role.skills == SkillList()


# ── load_subagent ─────────────────────────────────────────────────────────────


def test_load_subagent_loads_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("simpleharness.core._TOOLBOX_ROOT", tmp_path)
    _write_md(
        tmp_path / "subagents" / "helper.md",
        ("name: helper\ndescription: Helps out.\ntools:\n  - Read\n  - Grep\n"),
        "You are a helper subagent.",
    )
    sa = load_subagent("helper")
    assert sa.name == "helper"
    assert sa.description == "Helps out."
    assert sa.tools == ("Read", "Grep")
    assert sa.body == "You are a helper subagent."
    assert sa.skills == SkillList()


def test_load_subagent_missing_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("simpleharness.core._TOOLBOX_ROOT", tmp_path)
    (tmp_path / "subagents").mkdir()
    with pytest.raises(FileNotFoundError, match="ghost"):
        load_subagent("ghost")


def test_load_subagent_mcp_permission_handler_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("simpleharness.core._TOOLBOX_ROOT", tmp_path)
    _write_md(
        tmp_path / "subagents" / "bad.md",
        "invocation: mcp-permission-handler\n",
    )
    with pytest.raises(ValueError, match="mcp-permission-handler"):
        load_subagent("bad")


# ── load_all_subagents ────────────────────────────────────────────────────────


def test_load_all_subagents_no_dir_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("simpleharness.core._TOOLBOX_ROOT", tmp_path)
    result = load_all_subagents()
    assert result == ()


def test_load_all_subagents_returns_sorted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("simpleharness.core._TOOLBOX_ROOT", tmp_path)
    for stem in ("beta", "alpha", "gamma"):
        _write_md(
            tmp_path / "subagents" / f"{stem}.md",
            f"name: {stem}\n",
            f"Body of {stem}.",
        )
    result = load_all_subagents()
    assert len(result) == 3
    assert [sa.name for sa in result] == ["alpha", "beta", "gamma"]
