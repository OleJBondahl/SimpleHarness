"""Pure functions for approver verdict parsing and prompt building.

No I/O, no subprocess, no env access. All side-effect-free helpers extracted
from approver_shell.py so they can be tested and reasoned about in isolation.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shlex
from pathlib import Path
from typing import Any

import deal

# ────────────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────────────

_WRAPPER_COMMANDS: frozenset[str] = frozenset(
    {"sudo", "doas", "env", "time", "nice", "ionice", "xargs", "nohup", "timeout"}
)

_WRAPPER_FLAG_RE = re.compile(r"^-")
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Defense-in-depth: SIMPLEHARNESS_TASK_SLUG is meant to be the kebab-case
# slug produced by ``simpleharness new``. Reject anything that could
# traverse out of the task dir (``..``, ``/``, ``\``) before we use it
# to construct paths.
_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")

_WRAPPER_FLAGS_WITH_VALUE: frozenset[str] = frozenset(
    {"-u", "-g", "-h", "-p", "-D", "-C", "-T", "-r", "-t", "--user", "--group"}
)

# Last fenced JSON code block wins — models sometimes show a draft in
# an earlier block then commit to a final one at the bottom.
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL | re.IGNORECASE)

_POSIX_SHELL = os.name != "nt"

# ────────────────────────────────────────────────────────────────────────────
# Dataclasses
# ────────────────────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class ApproverEnv:
    """Runtime environment context for the approver hook."""

    worksite: Path
    task_slug: str
    role: str
    approver_model: str
    stream_log: Path | None
    fake: bool
    timeout_s: float


@dataclasses.dataclass(frozen=True)
class Verdict:
    """Approver decision with the matched pattern and reason."""

    decision: str  # "allow" | "deny"
    pattern: str  # "" on deny
    reason: str


# ────────────────────────────────────────────────────────────────────────────
# Pure functions
# ────────────────────────────────────────────────────────────────────────────


@deal.pure
def unwrap_wrappers(tokens: list[str], max_depth: int = 8) -> list[str]:
    """Strip sudo/env/time-style wrappers from a tokenized command.

    Returns the tokens starting at the real base command. Bounded
    recursion so pathological `sudo sudo sudo ...` cannot loop.
    """
    cur = tokens[:]
    for _ in range(max_depth):
        if not cur:
            return cur
        head = cur[0]
        if head not in _WRAPPER_COMMANDS:
            return cur
        cur = cur[1:]
        while cur:
            tok = cur[0]
            if tok in _WRAPPER_FLAGS_WITH_VALUE:
                cur = cur[2:] if len(cur) >= 2 else []
                continue
            if _WRAPPER_FLAG_RE.match(tok) or _ENV_ASSIGN_RE.match(tok):
                cur = cur[1:]
                continue
            break
    return cur


@deal.pure
def command_signature(command: str) -> str:
    """Return the base-command signature for e.g. FAKE-mode pattern synthesis.

    Uses shlex.split(posix=False) on Windows for better handling of
    backslashes; then unwraps sudo/env/etc. Always returns a non-empty
    string so the caller can always key off it.
    """
    if not isinstance(command, str):
        return "Bash"
    raw = command.strip()
    if not raw:
        return "Bash"
    try:
        parts = shlex.split(raw, posix=_POSIX_SHELL)
    except ValueError:
        parts = raw.split()
    if not parts:
        return "Bash"
    unwrapped = unwrap_wrappers(parts)
    if unwrapped:
        return unwrapped[0]
    return parts[0]


@deal.pure
def _deny_synthetic(reason: str) -> Verdict:
    return Verdict(decision="deny", pattern="", reason=reason)


@deal.pure
def _extract_raw_json(final_message: str) -> str | None:
    """Extract the last JSON block string from an approver message.

    Returns the raw JSON string (not yet parsed) if found, or None if no
    JSON block can be located. Handles both fenced code blocks and bare
    top-level objects.

    Args:
        final_message: The full text returned by the approver LLM.

    Returns:
        The raw JSON string to parse, or None if nothing was found.
    """
    matches = _JSON_BLOCK_RE.findall(final_message)
    if not matches:
        tail = final_message.strip()
        if tail.startswith("{") and tail.endswith("}"):
            return tail
        return None
    return matches[-1].strip()


@deal.pure
def _validate_verdict_fields(data: dict[str, object]) -> Verdict | None:
    """Validate the parsed verdict dict and return a Verdict or a synthetic deny.

    Returns a proper Verdict if all fields are valid, or a synthetic deny
    Verdict with a diagnostic reason if any field is missing or malformed.

    Args:
        data: A dict parsed from the approver's JSON block.

    Returns:
        A Verdict (allow or deny) built from the data, or a synthetic deny
        Verdict describing the first validation failure found.
    """
    decision = data.get("decision")
    pattern = data.get("pattern", "")
    reason = data.get("reason", "")

    if decision not in ("allow", "deny"):
        return _deny_synthetic(f"approver verdict had invalid decision: {decision!r}")
    if not isinstance(pattern, str):
        return _deny_synthetic("approver verdict pattern was not a string")
    if not isinstance(reason, str):
        return _deny_synthetic("approver verdict reason was not a string")
    if decision == "allow" and not pattern.strip():
        return _deny_synthetic("approver allow verdict had empty pattern")
    if not reason.strip():
        return _deny_synthetic("approver verdict had empty reason")
    return None


@deal.pure
def parse_verdict(final_message: str) -> Verdict:
    """Extract and validate the approver's JSON verdict.

    On any parse failure (no block, bad JSON, missing fields, invalid
    decision enum, non-string reason), returns a synthetic deny Verdict
    with a diagnostic reason. Never raises.
    """
    if not isinstance(final_message, str) or not final_message.strip():
        return _deny_synthetic("approver returned empty message")

    raw = _extract_raw_json(final_message)
    if raw is None:
        snippet = final_message.strip()[-120:]
        return _deny_synthetic(f"approver returned malformed verdict: {snippet!r}")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return _deny_synthetic(f"approver returned malformed verdict: {e}")

    if not isinstance(data, dict):
        return _deny_synthetic("approver verdict was not a JSON object")

    validation_error = _validate_verdict_fields(data)
    if validation_error is not None:
        return validation_error

    decision = data.get("decision")
    pattern = data.get("pattern", "")
    reason = data.get("reason", "")
    return Verdict(decision=decision, pattern=pattern, reason=reason)


@deal.pure
def build_approver_prompt(
    tool_name: str,
    tool_input: dict[str, Any],
    role: str,
    task_slug: str,
    stream_tail: str,
    currently_approved: list[str],
) -> str:
    """Assemble the compact prompt handed to the child ``claude -p`` approver."""
    input_pretty = json.dumps(tool_input, indent=2, sort_keys=True, default=str)
    if currently_approved:
        approved_block = "\n".join(f"- `{p}`" for p in currently_approved)
    else:
        approved_block = "(none — this worksite has no approver-added patterns yet)"
    return (
        "# Tool call awaiting approval\n"
        f"Tool: `{tool_name}`\n"
        "Arguments:\n"
        f"```json\n{input_pretty}\n```\n"
        "\n"
        "# Working agent context\n"
        f"Role: `{role}`\n"
        f"Task: `{task_slug}`\n"
        "\n"
        "## Recent assistant-text tail from the working agent's stream log\n"
        f"```\n{stream_tail}\n```\n"
        "\n"
        "# Currently approved extra_bash_allow patterns in this worksite\n"
        f"{approved_block}\n"
        "\n"
        "Now follow the approver role body and emit your single JSON verdict "
        "block as your final message.\n"
    )


@deal.pure
def fake_verdict_from_input(tool_name: str, tool_input: dict[str, Any]) -> Verdict:
    """FAKE-mode shortcut: invent an allow verdict keyed off the base command."""
    if tool_name == "Bash":
        sig = command_signature(
            tool_input.get("command", "") if isinstance(tool_input, dict) else ""
        )
        pattern = f"{sig} *"
    else:
        sig = tool_name
        pattern = sig
    return Verdict(decision="allow", pattern=pattern, reason=f"FAKE mode: auto-approved {sig}")


@deal.pure
def _extract_assistant_text(obj: Any) -> str:
    """Pull assistant text content out of one Claude Code stream-json event."""
    if not isinstance(obj, dict):
        return ""
    if obj.get("type") != "assistant":
        return ""
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            t = block.get("text")
            if isinstance(t, str) and t.strip():
                chunks.append(t.strip())
    return "\n".join(chunks)


@deal.pure
def _task_dir(worksite: Path, task_slug: str) -> Path:
    return worksite / "simpleharness" / "tasks" / task_slug


# ────────────────────────────────────────────────────────────────────────────
# Phase 3a — plan/finalize dataclasses
# ────────────────────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class SpawnRequest:
    """Everything the shell needs to call _spawn_approver."""

    prompt: str
    approver_model: str
    worksite: Path
    task_slug: str
    timeout_s: float


@dataclasses.dataclass(frozen=True)
class ReviewPlan:
    """Tagged union — exactly one of the three action fields is set.

    Shell inspects each field in priority order:
      1. ``preemptive_deny`` — short-circuit before spawning
      2. ``fake_verdict``    — use synthesised verdict, skip spawn
      3. ``spawn``           — run claude -p and parse result

    ``prompt`` is set whenever a prompt was built (fake or spawn paths)
    so the shell can include ``prompt_len`` in the fake log without
    re-computing the prompt itself.
    """

    preemptive_deny: str | None = None
    fake_verdict: Verdict | None = None
    spawn: SpawnRequest | None = None
    prompt: str = ""


@dataclasses.dataclass(frozen=True)
class ReviewOutcome:
    """Action intents returned by finalize_review — shell executes the side effects."""

    verdict: Verdict
    pattern_to_persist: str | None  # set when allow + pattern non-empty
    should_escalate: bool  # true when deny + escalate_denials_to_correction


# ────────────────────────────────────────────────────────────────────────────
# Pure planning functions
# ────────────────────────────────────────────────────────────────────────────


@deal.pure
def plan_review(
    env: ApproverEnv,
    tool_name: str,
    tool_input: dict[str, Any],
    cfg: Any,
    *,
    role_file_exists: bool,
    stream_tail: str,
    currently_approved: tuple[str, ...],
) -> ReviewPlan:
    """Decide what the shell should do — no I/O, no subprocess, no env reads.

    Returns a ReviewPlan with exactly one field set:
    - ``preemptive_deny`` if the role file is missing (belt-and-braces check)
    - ``fake_verdict``    if env.fake is True
    - ``spawn``           for the normal slow path
    """
    if not role_file_exists:
        role_path = Path("roles") / "approver.md"  # for a legible error message
        return ReviewPlan(preemptive_deny=f"approver role file not found at {role_path}")

    prompt = build_approver_prompt(
        tool_name=tool_name,
        tool_input=tool_input,
        role=env.role,
        task_slug=env.task_slug,
        stream_tail=stream_tail,
        currently_approved=list(currently_approved),
    )

    if env.fake:
        verdict = fake_verdict_from_input(tool_name, tool_input)
        return ReviewPlan(fake_verdict=verdict, prompt=prompt)

    spawn = SpawnRequest(
        prompt=prompt,
        approver_model=env.approver_model,
        worksite=env.worksite,
        task_slug=env.task_slug,
        timeout_s=env.timeout_s,
    )
    return ReviewPlan(spawn=spawn, prompt=prompt)


@deal.pure
def finalize_review(verdict: Verdict, cfg: Any) -> ReviewOutcome:
    """Map a parsed Verdict to action intents the shell will execute.

    Pure: no file access, no subprocess, no env reads.

    - ``pattern_to_persist`` is set iff decision is "allow" and pattern is non-empty.
    - ``should_escalate``    is set iff decision is "deny" and the config flag is on.
    """
    pattern_to_persist: str | None = None
    if verdict.decision == "allow" and verdict.pattern.strip():
        pattern_to_persist = verdict.pattern

    should_escalate = verdict.decision == "deny" and cfg.permissions.escalate_denials_to_correction
    return ReviewOutcome(
        verdict=verdict,
        pattern_to_persist=pattern_to_persist,
        should_escalate=should_escalate,
    )
