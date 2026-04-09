"""Stop/SubagentStop hook: enforce that required skills were invoked.

Invoked by Claude Code as:
    uv run python -m simpleharness.hooks.enforce_must_use

Reads on stdin a JSON object from Claude Code containing at minimum:
    transcript_path   — path to the session JSONL
    hook_event_name   — "Stop" or "SubagentStop"
    (SubagentStop may also carry a subagent identifier we can use)

Reads env vars set by the harness:
    SIMPLEHARNESS_MUST_USE_MAIN  — JSON list of skill/tool names required
                                    before a main-role Stop succeeds
    SIMPLEHARNESS_MUST_USE_SUB   — JSON object mapping subagent name → list
                                    of required names (for SubagentStop)
    SIMPLEHARNESS_ENFORCEMENT    — strict | warn | off

Exit codes:
    0 — all required skills were invoked (or enforcement is not strict)
    2 — BLOCKED: some required skills missing AND enforcement=strict.
        Writes a message to stderr that Claude Code feeds back to the agent.

In warn mode, writes the missing list to stderr but exits 0 (non-blocking).
In off mode, immediately exits 0 without reading the transcript.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import deal

from simpleharness.transcript import check_required_invocations, read_transcript_jsonl


@deal.pure
def decide_enforcement(
    missing: tuple[str, ...],
    role_or_subagent_name: str,
    enforcement_mode: str,  # "strict" | "warn" | "off"
) -> tuple[int, str]:
    """Return (exit_code, stderr_message) for the hook.

    - off → (0, "")
    - no missing → (0, "")
    - missing + warn → (0, "WARN: ...")
    - missing + strict → (2, "BLOCKED by SimpleHarness: ...")
    """
    if enforcement_mode == "off" or not missing:
        return (0, "")
    missing_str = ", ".join(missing)
    if enforcement_mode == "warn":
        return (
            0,
            f"WARN: {role_or_subagent_name} should have invoked these skills before finishing: {missing_str}",
        )
    # strict
    return (
        2,
        f"BLOCKED by SimpleHarness: {role_or_subagent_name} requires these skills to be invoked before finishing: {missing_str}. Invoke them now, then continue.",
    )


@deal.pure
def pick_required_list(
    event_name: str,
    subagent_name: str,
    must_use_main: tuple[str, ...],
    must_use_sub: dict[str, tuple[str, ...]],
) -> tuple[str, tuple[str, ...]]:
    """Given the hook event and subagent name, return (label, required_names).

    - event_name == "Stop" → (subagent_name, must_use_main)
    - event_name == "SubagentStop" → (subagent_name, must_use_sub.get(subagent_name, ()))
    - anything else → (subagent_name, ())
    """
    if event_name == "Stop":
        return (subagent_name, must_use_main)
    if event_name == "SubagentStop":
        return (subagent_name, must_use_sub.get(subagent_name, ()))
    return (subagent_name, ())


def main() -> int:  # pragma: no cover
    enforcement = os.environ.get("SIMPLEHARNESS_ENFORCEMENT", "strict")
    if enforcement == "off":
        return 0

    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0  # can't parse input, don't block

    event_name = str(hook_input.get("hook_event_name", ""))
    transcript_path_str = str(hook_input.get("transcript_path", ""))

    if event_name == "Stop":
        label = os.environ.get("SIMPLEHARNESS_ROLE", "unknown")
    else:
        label = str(hook_input.get("name", hook_input.get("subagent", "unknown")))

    try:
        must_use_main = tuple(json.loads(os.environ.get("SIMPLEHARNESS_MUST_USE_MAIN", "[]")))
        must_use_sub_raw = json.loads(os.environ.get("SIMPLEHARNESS_MUST_USE_SUB", "{}"))
        must_use_sub = {k: tuple(v) for k, v in must_use_sub_raw.items()}
    except json.JSONDecodeError:
        return 0

    _, required = pick_required_list(event_name, label, must_use_main, must_use_sub)

    if not required:
        return 0

    events = read_transcript_jsonl(Path(transcript_path_str)) if transcript_path_str else ()
    missing = check_required_invocations(events, required)

    exit_code, message = decide_enforcement(missing, label, enforcement)
    if message:
        sys.stderr.write(message + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
