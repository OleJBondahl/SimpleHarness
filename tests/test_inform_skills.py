"""Tests for simpleharness.hooks.inform_skills pure functions."""

from __future__ import annotations

from simpleharness.hooks.inform_skills import build_reminder_text, build_session_start_payload

# ---------------------------------------------------------------------------
# build_reminder_text
# ---------------------------------------------------------------------------


def test_empty_role_and_empty_lists_returns_empty_string() -> None:
    result = build_reminder_text("", (), ())
    assert result == ""


def test_role_present_empty_lists_returns_empty_string() -> None:
    result = build_reminder_text("my-role", (), ())
    assert result == ""


def test_available_only_no_must_section() -> None:
    available = (
        {"name": "humanizer", "hint": "strip AI-voice tells"},
        {"name": "writing-skills", "hint": "for authoring new skill docs"},
    )
    result = build_reminder_text("documentation-writer", available, ())
    assert "Skills available to you (use when relevant):" in result
    assert "humanizer" in result
    assert "writing-skills" in result
    assert "MUST" not in result
    assert "Stop hook" not in result


def test_must_use_only_no_available_section() -> None:
    must_use = ("humanizer", "updating-memory")
    result = build_reminder_text("documentation-writer", (), must_use)
    assert "Skills available" not in result
    assert "Skills you MUST invoke" in result
    assert "humanizer" in result
    assert "updating-memory" in result
    assert "Stop hook will block you" in result


def test_both_populated_full_output() -> None:
    available = (
        {"name": "humanizer", "hint": "strip AI-voice tells before finalizing prose"},
        {"name": "writing-skills", "hint": "for authoring new skill docs"},
    )
    must_use = ("humanizer", "updating-memory")
    result = build_reminder_text("documentation-writer", available, must_use)
    assert result.startswith("Role: documentation-writer")
    assert "Skills available to you (use when relevant):" in result
    assert "Skills you MUST invoke before declaring this task complete:" in result
    assert "humanizer \u2014 strip AI-voice tells before finalizing prose" in result
    assert "  - updating-memory" in result
    assert "Stop hook will block you" in result


def test_skill_with_empty_hint_renders_without_em_dash() -> None:
    available = ({"name": "my-skill", "hint": ""},)
    result = build_reminder_text("role", available, ())
    assert "  - my-skill" in result
    assert "\u2014" not in result


def test_empty_role_name_shows_unknown() -> None:
    available = ({"name": "some-skill", "hint": "hint text"},)
    result = build_reminder_text("", available, ())
    assert result.startswith("Role: unknown")


# ---------------------------------------------------------------------------
# build_session_start_payload
# ---------------------------------------------------------------------------


def test_build_session_start_payload_shape() -> None:
    result = build_session_start_payload("foo")
    assert result == {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "foo",
        }
    }


def test_build_session_start_payload_empty_string() -> None:
    result = build_session_start_payload("")
    assert result["hookSpecificOutput"]["additionalContext"] == ""
