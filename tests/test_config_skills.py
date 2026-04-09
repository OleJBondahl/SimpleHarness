"""Tests for SkillsConfig loading in load_config."""

from __future__ import annotations

from pathlib import Path

import pytest

from simpleharness.core import Skill, SkillsConfig
from simpleharness.shell import load_config


def _write_config(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ── no skills section ─────────────────────────────────────────────────────────


def test_no_skills_section_uses_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("simpleharness.core._TOOLBOX_ROOT", tmp_path)
    _write_config(tmp_path / "config.yaml", "model: opus\n")
    config = load_config(tmp_path)
    assert config.skills == SkillsConfig()


# ── full skills section ────────────────────────────────────────────────────────


def test_full_skills_section(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("simpleharness.core._TOOLBOX_ROOT", tmp_path)
    _write_config(
        tmp_path / "config.yaml",
        (
            "skills:\n"
            "  default_available:\n"
            "    - name: humanizer\n"
            "      hint: strip AI-voice tells\n"
            "  default_must_use:\n"
            "    - updating-memory\n"
            "  enforcement: warn\n"
        ),
    )
    config = load_config(tmp_path)
    assert config.skills.default_available == (Skill("humanizer", "strip AI-voice tells"),)
    assert config.skills.default_must_use == ("updating-memory",)
    assert config.skills.enforcement == "warn"


# ── invalid enforcement ────────────────────────────────────────────────────────


def test_invalid_enforcement_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("simpleharness.core._TOOLBOX_ROOT", tmp_path)
    _write_config(
        tmp_path / "config.yaml",
        "skills:\n  enforcement: bogus\n",
    )
    with pytest.raises(ValueError, match=r"skills\.enforcement"):
        load_config(tmp_path)


# ── mixed bare strings and dicts in default_available ─────────────────────────


def test_default_available_mixed_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("simpleharness.core._TOOLBOX_ROOT", tmp_path)
    _write_config(
        tmp_path / "config.yaml",
        (
            "skills:\n"
            "  default_available:\n"
            "    - plain-skill\n"
            "    - name: rich-skill\n"
            "      hint: with a hint\n"
        ),
    )
    config = load_config(tmp_path)
    assert config.skills.default_available == (
        Skill("plain-skill", ""),
        Skill("rich-skill", "with a hint"),
    )


# ── default_must_use not a list ────────────────────────────────────────────────


def test_default_must_use_not_list_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("simpleharness.core._TOOLBOX_ROOT", tmp_path)
    _write_config(
        tmp_path / "config.yaml",
        "skills:\n  default_must_use: not-a-list\n",
    )
    with pytest.raises(ValueError, match="must_use"):
        load_config(tmp_path)
