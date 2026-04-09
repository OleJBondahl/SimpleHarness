"""Tests for simpleharness.transcript — pure transcript parsing helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from simpleharness.transcript import (
    check_required_invocations,
    find_skill_invocations,
    find_tool_invocations,
    iter_tool_uses,
    read_transcript_jsonl,
)

# ── Factories ─────────────────────────────────────────────────────────────────


def _assistant_event(*tool_uses: dict[str, Any], extra_text: bool = False) -> dict[str, Any]:
    """Build a synthetic assistant event with optional tool_use blocks."""
    content: list[dict[str, Any]] = []
    if extra_text:
        content.append({"type": "text", "text": "some text"})
    for tu in tool_uses:
        content.append({"type": "tool_use", **tu})
    return {"type": "assistant", "message": {"role": "assistant", "content": content}}


def _user_event() -> dict[str, Any]:
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]},
    }


def _skill_tool_use(skill_name: str) -> dict[str, Any]:
    return {"name": "Skill", "id": "toolu_01", "input": {"skill": skill_name}}


def _tool_use(name: str, **inp: Any) -> dict[str, Any]:
    return {"name": name, "id": "toolu_02", "input": dict(inp)}


def _write_jsonl(path: Path, lines: list[Any]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for obj in lines:
            fh.write(json.dumps(obj) + "\n")


# ── read_transcript_jsonl ─────────────────────────────────────────────────────


def test_read_nonexistent_path(tmp_path: Path) -> None:
    result = read_transcript_jsonl(tmp_path / "no_such_file.jsonl")
    assert result == ()


def test_read_well_formed(tmp_path: Path) -> None:
    events = [{"type": "system"}, {"type": "assistant"}, {"type": "user"}]
    p = tmp_path / "t.jsonl"
    _write_jsonl(p, events)
    result = read_transcript_jsonl(p)
    assert len(result) == 3
    assert result[0]["type"] == "system"
    assert result[2]["type"] == "user"


def test_read_skips_malformed_line(tmp_path: Path) -> None:
    p = tmp_path / "t.jsonl"
    with p.open("w") as fh:
        fh.write(json.dumps({"type": "system"}) + "\n")
        fh.write("NOT JSON {\n")
        fh.write(json.dumps({"type": "user"}) + "\n")
    result = read_transcript_jsonl(p)
    assert len(result) == 2
    assert result[0]["type"] == "system"
    assert result[1]["type"] == "user"


def test_read_skips_blank_lines(tmp_path: Path) -> None:
    p = tmp_path / "t.jsonl"
    with p.open("w") as fh:
        fh.write("\n")
        fh.write(json.dumps({"type": "system"}) + "\n")
        fh.write("   \n")
        fh.write(json.dumps({"type": "user"}) + "\n")
    result = read_transcript_jsonl(p)
    assert len(result) == 2


# ── iter_tool_uses ────────────────────────────────────────────────────────────


def test_iter_tool_uses_extracts_multiple() -> None:
    events = (
        _assistant_event(
            _skill_tool_use("humanizer"),
            _tool_use("Write", file_path="x.py", content=""),
            extra_text=True,
        ),
        _assistant_event(),  # no tool_uses
        _user_event(),  # must be ignored
        {"type": "assistant", "message": {"role": "assistant"}},  # missing content
    )
    result = iter_tool_uses(events)
    assert len(result) == 2
    assert result[0]["name"] == "Skill"
    assert result[1]["name"] == "Write"


def test_iter_tool_uses_ignores_non_assistant() -> None:
    events = (_user_event(),)
    assert iter_tool_uses(events) == ()


def test_iter_tool_uses_skips_missing_content() -> None:
    malformed = {"type": "assistant", "message": {"role": "assistant"}}
    result = iter_tool_uses((malformed,))
    assert result == ()


# ── find_skill_invocations ────────────────────────────────────────────────────


def test_find_skill_invocations_returns_skill_names() -> None:
    events = (_assistant_event(_skill_tool_use("humanizer"), _skill_tool_use("brainstorming")),)
    result = find_skill_invocations(events)
    assert result == frozenset({"humanizer", "brainstorming"})


def test_find_skill_invocations_ignores_non_skill_tools() -> None:
    # A tool named "Write" with a "skill" key in input — should NOT be picked up
    events = (_assistant_event(_tool_use("Write", skill="sneaky", file_path="f.py", content="")),)
    result = find_skill_invocations(events)
    assert result == frozenset()


# ── find_tool_invocations ─────────────────────────────────────────────────────


def test_find_tool_invocations_returns_intersection() -> None:
    events = (
        _assistant_event(
            _tool_use("mcp__context7__query-docs"),
            _tool_use("Write", file_path="x"),
        ),
    )
    wanted = frozenset({"mcp__context7__query-docs", "mcp__other__tool"})
    result = find_tool_invocations(events, wanted)
    assert result == frozenset({"mcp__context7__query-docs"})


def test_find_tool_invocations_empty_tool_names() -> None:
    events = (_assistant_event(_tool_use("Write", file_path="x")),)
    result = find_tool_invocations(events, frozenset())
    assert result == frozenset()


# ── check_required_invocations ────────────────────────────────────────────────


def test_check_required_one_missing() -> None:
    events = (
        _assistant_event(
            _skill_tool_use("humanizer"),
            _tool_use("mcp__context7__query-docs"),
        ),
    )
    required = ("humanizer", "mcp__context7__query-docs", "missing-skill")
    result = check_required_invocations(events, required)
    assert result == ("missing-skill",)


def test_check_required_all_satisfied() -> None:
    events = (
        _assistant_event(
            _skill_tool_use("humanizer"),
            _tool_use("Write", file_path="x"),
        ),
    )
    result = check_required_invocations(events, ("humanizer", "Write"))
    assert result == ()


def test_check_required_none_satisfied() -> None:
    events = (_assistant_event(_tool_use("Read", file_path="x")),)
    required = ("humanizer", "brainstorming", "mcp__foo__bar")
    result = check_required_invocations(events, required)
    assert result == required


def test_check_required_preserves_order() -> None:
    events = ()
    required = ("c", "a", "b")
    result = check_required_invocations(events, required)
    assert result == ("c", "a", "b")


def test_check_required_satisfied_by_both_paths() -> None:
    # "humanizer" appears both as a Skill invocation AND as a direct tool named "humanizer"
    events = (
        _assistant_event(
            _skill_tool_use("humanizer"),
            _tool_use("humanizer"),
        ),
    )
    result = check_required_invocations(events, ("humanizer",))
    assert result == ()
