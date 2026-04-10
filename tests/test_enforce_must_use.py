"""Tests for simpleharness.hooks.enforce_must_use pure functions + subprocess wiring."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING, Any

from simpleharness.hooks.enforce_must_use import decide_enforcement, pick_required_list

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# decide_enforcement
# ---------------------------------------------------------------------------


def test_no_missing_strict_returns_zero() -> None:
    code, msg = decide_enforcement((), "role", "strict")
    assert code == 0
    assert msg == ""


def test_missing_off_returns_zero() -> None:
    code, msg = decide_enforcement(("humanizer",), "role", "off")
    assert code == 0
    assert msg == ""


def test_missing_warn_returns_zero_with_warn_message() -> None:
    code, msg = decide_enforcement(("humanizer",), "role", "warn")
    assert code == 0
    assert msg.startswith("WARN:")
    assert "role" in msg
    assert "humanizer" in msg


def test_missing_strict_returns_two_with_block_message() -> None:
    code, msg = decide_enforcement(
        ("humanizer", "updating-memory"), "documentation-writer", "strict"
    )
    assert code == 2
    assert msg.startswith("BLOCKED by SimpleHarness:")
    assert "humanizer" in msg
    assert "updating-memory" in msg
    # Both names should appear comma-separated
    assert "humanizer, updating-memory" in msg


# ---------------------------------------------------------------------------
# pick_required_list
# ---------------------------------------------------------------------------


def test_pick_stop_returns_main_list() -> None:
    label, required = pick_required_list("Stop", "docwriter", ("a", "b"), {"review": ("x",)})
    assert label == "docwriter"
    assert required == ("a", "b")


def test_pick_subagent_stop_returns_sub_list() -> None:
    label, required = pick_required_list("SubagentStop", "review", ("a",), {"review": ("x", "y")})
    assert label == "review"
    assert required == ("x", "y")


def test_pick_subagent_stop_unknown_subagent_returns_empty() -> None:
    label, required = pick_required_list("SubagentStop", "unknown", (), {})
    assert label == "unknown"
    assert required == ()


def test_pick_unknown_event_returns_empty() -> None:
    label, required = pick_required_list("weird", "x", (), {})
    assert label == "x"
    assert required == ()


# ---------------------------------------------------------------------------
# Subprocess end-to-end test
# ---------------------------------------------------------------------------


def _make_transcript(tmp_path: Path, skill_invoked: str | None) -> Path:
    """Write a minimal transcript JSONL. Optionally include a Skill invocation."""
    transcript = tmp_path / "transcript.jsonl"
    events: list[dict[str, Any]] = []
    if skill_invoked:
        events.append(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Skill",
                            "id": "toolu_01",
                            "input": {"skill": skill_invoked},
                        }
                    ],
                },
            }
        )
    with transcript.open("w", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event) + "\n")
    return transcript


def _run_hook(
    tmp_path: Path,
    transcript: Path,
    env_extra: dict[str, str],
    hook_input: dict[str, Any],
) -> subprocess.CompletedProcess[str]:
    import os

    env = {**os.environ, **env_extra}
    return subprocess.run(
        [sys.executable, "-m", "simpleharness.hooks.enforce_must_use"],
        input=json.dumps(hook_input),
        capture_output=True,
        text=True,
        env=env,
    )


def test_subprocess_required_skill_invoked_exits_zero(tmp_path: Path) -> None:
    transcript = _make_transcript(tmp_path, skill_invoked="humanizer")
    result = _run_hook(
        tmp_path,
        transcript,
        env_extra={
            "SIMPLEHARNESS_ENFORCEMENT": "strict",
            "SIMPLEHARNESS_ROLE": "documentation-writer",
            "SIMPLEHARNESS_MUST_USE_MAIN": json.dumps(["humanizer"]),
            "SIMPLEHARNESS_MUST_USE_SUB": "{}",
        },
        hook_input={
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
        },
    )
    assert result.returncode == 0


def test_subprocess_required_skill_missing_exits_two(tmp_path: Path) -> None:
    transcript = _make_transcript(tmp_path, skill_invoked=None)
    result = _run_hook(
        tmp_path,
        transcript,
        env_extra={
            "SIMPLEHARNESS_ENFORCEMENT": "strict",
            "SIMPLEHARNESS_ROLE": "documentation-writer",
            "SIMPLEHARNESS_MUST_USE_MAIN": json.dumps(["humanizer"]),
            "SIMPLEHARNESS_MUST_USE_SUB": "{}",
        },
        hook_input={
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
        },
    )
    assert result.returncode == 2
    assert "BLOCKED by SimpleHarness:" in result.stderr
    assert "humanizer" in result.stderr
