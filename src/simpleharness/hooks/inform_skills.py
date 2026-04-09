"""SessionStart hook: inject a skill-awareness reminder into the agent's context.

Invoked by Claude Code as:
    uv run python -m simpleharness.hooks.inform_skills

Reads these env vars (set by the harness before spawning the claude -p session):
    SIMPLEHARNESS_ROLE             — the active main role name
    SIMPLEHARNESS_AVAILABLE_SKILLS — JSON list of {name, hint} dicts
    SIMPLEHARNESS_MUST_USE_MAIN    — JSON list of skill-name strings

Emits to stdout a JSON object shaped for the SessionStart hook:
    {"hookSpecificOutput": {"hookEventName": "SessionStart",
                            "additionalContext": "<rendered reminder>"}}

If env vars are missing or empty, emits an empty additionalContext (no-op).
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import deal


@deal.pure
def build_reminder_text(
    role_name: str,
    available: tuple[dict[str, str], ...],
    must_use: tuple[str, ...],
) -> str:
    """Render the reminder text that gets injected as additionalContext.

    Empty ``available`` and empty ``must_use`` → returns empty string (caller
    should skip emitting any reminder).
    """
    if not available and not must_use:
        return ""

    display_role = role_name if role_name else "unknown"
    lines: list[str] = [f"Role: {display_role}"]

    if available:
        lines.append("")
        lines.append("Skills available to you (use when relevant):")
        for skill in available:
            name = skill.get("name", "")
            hint = skill.get("hint", "")
            if hint:
                lines.append(f"  - {name} \u2014 {hint}")
            else:
                lines.append(f"  - {name}")

    if must_use:
        lines.append("")
        lines.append("Skills you MUST invoke before declaring this task complete:")
        for name in must_use:
            lines.append(f"  - {name}")
        lines.append("")
        lines.append(
            "If you stop without invoking these, the Stop hook will block you and send you"
        )
        lines.append("back with a reminder.")

    return "\n".join(lines)


@deal.pure
def build_session_start_payload(reminder: str) -> dict[str, Any]:
    """Wrap a reminder string in the SessionStart hook output shape."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": reminder,
        }
    }


def main() -> int:  # pragma: no cover - exercised via subprocess test
    role = os.environ.get("SIMPLEHARNESS_ROLE", "")
    available_raw = os.environ.get("SIMPLEHARNESS_AVAILABLE_SKILLS", "[]")
    must_use_raw = os.environ.get("SIMPLEHARNESS_MUST_USE_MAIN", "[]")
    try:
        available_list = json.loads(available_raw)
        must_use_list = json.loads(must_use_raw)
    except json.JSONDecodeError:
        available_list = []
        must_use_list = []
    available = tuple(
        {"name": str(a.get("name", "")), "hint": str(a.get("hint", ""))}
        for a in available_list
        if isinstance(a, dict)
    )
    must_use = tuple(str(m) for m in must_use_list if isinstance(m, str))
    reminder = build_reminder_text(role, available, must_use)
    payload = build_session_start_payload(reminder)
    sys.stdout.write(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
