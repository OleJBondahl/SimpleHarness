"""Helpers for reading and querying Claude Code session transcript JSONL files.

File I/O is isolated to ``read_transcript_jsonl``; all other helpers are pure
functions over the returned tuple of event dicts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import deal


@deal.has("io")
def read_transcript_jsonl(path: Path) -> tuple[dict[str, Any], ...]:
    """Read a Claude Code transcript JSONL file.

    Returns a tuple of parsed event dicts, one per non-empty line.
    Malformed lines are silently skipped — the transcript may contain
    partial writes if the session is still in progress.
    If the file doesn't exist, returns an empty tuple.
    """
    if not path.exists():
        return ()
    events: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                events.append(obj)
    return tuple(events)


@deal.pure
def iter_tool_uses(events: tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], ...]:
    """Extract every tool_use block from every assistant event.

    Returns a tuple of dicts with keys {'name': str, 'input': dict}.
    Skips events that aren't assistant messages or don't contain tool_use blocks.
    Defensive: if any field is missing or the wrong shape, that event is skipped.
    """
    results: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("type") != "assistant":
            continue
        msg = event.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_use":
                continue
            name = block.get("name")
            inp = block.get("input")
            if not isinstance(name, str) or not isinstance(inp, dict):
                continue
            results.append({"name": name, "input": inp})
    return tuple(results)


@deal.pure
def find_skill_invocations(events: tuple[dict[str, Any], ...]) -> frozenset[str]:
    """Return the set of skill names invoked via the Skill tool in this transcript.

    A skill invocation looks like: tool_use.name == "Skill" and tool_use.input has
    a "skill" field pointing to the skill name. Missing/malformed entries are
    silently skipped.
    """
    names: set[str] = set()
    for tool in iter_tool_uses(events):
        if tool["name"] != "Skill":
            continue
        skill_name = tool["input"].get("skill")
        if isinstance(skill_name, str) and skill_name:
            names.add(skill_name)
    return frozenset(names)


@deal.pure
def find_tool_invocations(
    events: tuple[dict[str, Any], ...], tool_names: frozenset[str]
) -> frozenset[str]:
    """Return the subset of tool_names that appear in the transcript.

    Matches on exact tool name (e.g. 'mcp__context7__query-docs'). Useful for
    checking whether specific MCP tools were invoked.
    """
    if not tool_names:
        return frozenset()
    invoked = {tool["name"] for tool in iter_tool_uses(events)}
    return frozenset(tool_names & invoked)


@deal.pure
def check_required_invocations(
    events: tuple[dict[str, Any], ...],
    required_names: tuple[str, ...],
) -> tuple[str, ...]:
    """Return the names from required_names that are MISSING from the transcript.

    A name is considered satisfied if it was invoked as a Skill (via the Skill
    tool with that skill name in input.skill) OR as a direct tool call with that
    name. This lets the same required-list cover both skills and MCP tools.
    Order of the returned missing names matches the order they appear in required_names.
    """
    skill_names = find_skill_invocations(events)
    direct_names = {tool["name"] for tool in iter_tool_uses(events)}
    satisfied = skill_names | direct_names
    return tuple(name for name in required_names if name not in satisfied)
