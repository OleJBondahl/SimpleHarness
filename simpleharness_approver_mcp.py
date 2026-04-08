"""SimpleHarness approver-mode MCP server.

Exposes one MCP tool — ``review`` — that Claude Code invokes via
``--permission-prompt-tool mcp__simpleharness_approver__review`` whenever the
working agent attempts a tool call that is not on the static allowlist.

The tool:
  1. Reads the tail of the working agent's .jsonl stream log for context.
  2. Loads the approver role body and the worksite's current allow list.
  3. Spawns a short-lived ``claude -p`` child running the approver role.
  4. Parses the final JSON verdict ({decision, pattern, reason}).
  5. On allow: appends ``pattern`` to the worksite config.yaml and returns
     ``{"behavior": "allow", "updatedInput": <tool_input>}``.
  6. On deny: returns ``{"behavior": "deny", "message": <reason>}`` and
     optionally appends a note to the task's CORRECTION.md.

The MCP server is a short-lived subprocess spawned by ``claude -p`` itself
(stdio transport). In-process state (the approval cache) lives for the
duration of the working agent's session. Persistent approvals live in
``<worksite>/simpleharness/config.yaml``.

This module must never print to stdout — that channel is reserved for MCP
protocol traffic. Diagnostic output goes to stderr and to the per-invocation
approver log file.
"""

from __future__ import annotations

import datetime as _dt
import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from simpleharness_core import (
    append_approved_pattern,
    load_config,
    load_role,
    toolbox_root,
)

# ────────────────────────────────────────────────────────────────────────────
# Module-level state
# ────────────────────────────────────────────────────────────────────────────

# In-process approval cache. Key: normalized command signature
# (first whitespace token for Bash, tool name otherwise). Value: the glob
# pattern that was approved earlier in this session. Lives only for the
# lifetime of this MCP server process.
_APPROVAL_CACHE: dict[str, str] = {}

# Counter incremented each time FAKE mode short-circuits to the real approver
# path, used by the verification script to assert cache short-circuits work.
_FAKE_CALL_COUNT = 0


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────


def _stderr(msg: str) -> None:
    """Write a diagnostic line to stderr. Stdout is reserved for MCP traffic."""
    print(f"[approver-mcp] {msg}", file=sys.stderr, flush=True)


def _now_stamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _command_signature(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Normalize a tool call to a short cache key.

    For Bash calls, the key is the first whitespace-separated token of the
    command (the base executable). For every other tool, the key is the tool
    name itself — one approval per non-Bash tool name per session is plenty.
    """
    if tool_name == "Bash":
        raw = tool_input.get("command", "")
        if not isinstance(raw, str):
            return "Bash"
        raw = raw.strip()
        if not raw:
            return "Bash"
        try:
            parts = shlex.split(raw, posix=(os.name != "nt"))
        except ValueError:
            parts = raw.split()
        return parts[0] if parts else "Bash"
    return tool_name


def _tool_input_as_string(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Pick the single string we pattern-match against for cache hits.

    For Bash, the full command. For other tools, compact JSON of the input.
    """
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        return cmd if isinstance(cmd, str) else json.dumps(tool_input, sort_keys=True)
    return json.dumps(tool_input, sort_keys=True)


def read_stream_tail(log_path: Path | str | None, n_lines: int = 30) -> str:
    """Return the last ``n_lines`` non-empty assistant-text lines from the
    working agent's .jsonl stream log.

    Defensive: file may not exist yet, may be mid-write, may contain partial
    or invalid JSON. On any failure, returns a short fallback string so the
    approver always gets *something* to read.
    """
    if not log_path:
        return "(no stream context available)"
    try:
        p = Path(log_path)
        if not p.exists() or not p.is_file():
            return "(no stream context available)"
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "(no stream context available)"
    except Exception:
        return "(no stream context available)"

    out: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = _extract_assistant_text(obj)
        if text:
            out.append(text)

    if not out:
        return "(no stream context available)"
    tail = out[-n_lines:]
    return "\n".join(tail)


def _extract_assistant_text(obj: Any) -> str:
    """Pull assistant text content out of one Claude Code stream-json event.

    Claude Code emits events like::

        {"type":"assistant","message":{"role":"assistant","content":[
            {"type":"text","text":"..."}, {"type":"tool_use", ...}]}}

    Returns the concatenated text blocks or an empty string.
    """
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


# ────────────────────────────────────────────────────────────────────────────
# Verdict parsing
# ────────────────────────────────────────────────────────────────────────────

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)


def _deny_synthetic(reason: str) -> dict[str, str]:
    return {"decision": "deny", "pattern": "", "reason": reason}


def parse_verdict(final_message: str) -> dict[str, str]:
    """Extract the LAST JSON code block from the approver's final message,
    parse it, validate required fields. Return a synthetic deny verdict on
    any failure — never raise.
    """
    if not isinstance(final_message, str) or not final_message.strip():
        return _deny_synthetic("approver returned empty message")

    matches = _JSON_BLOCK_RE.findall(final_message)
    if not matches:
        # Fall back: maybe the model returned a bare JSON object as the tail.
        tail = final_message.strip()
        if tail.startswith("{") and tail.endswith("}"):
            matches = [tail]
    if not matches:
        snippet = final_message.strip()[-120:]
        return _deny_synthetic(f"approver returned malformed verdict: {snippet!r}")

    raw = matches[-1].strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return _deny_synthetic(f"approver returned malformed verdict: {e}")

    if not isinstance(data, dict):
        return _deny_synthetic("approver verdict was not a JSON object")

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

    return {"decision": decision, "pattern": pattern, "reason": reason}


# ────────────────────────────────────────────────────────────────────────────
# Spawning the approver
# ────────────────────────────────────────────────────────────────────────────


def _approver_log_dir(worksite: Path, task_slug: str) -> Path:
    d = worksite / "simpleharness" / "logs" / task_slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def spawn_approver(
    prompt: str,
    approver_model: str,
    approver_role_path: Path,
    task_log_dir: Path,
) -> str:
    """Run ``claude -p`` with the approver role appended as system prompt,
    stream its JSON events to a per-invocation log file, and return the final
    assistant text as a plain string.
    """
    log_path = task_log_dir / f"approver-{_now_stamp()}.jsonl"

    cmd = [
        "claude",
        "-p",
        "--append-system-prompt-file",
        str(approver_role_path),
        "--model",
        approver_model,
        "--output-format",
        "stream-json",
        "--verbose",
        "--max-turns",
        "3",
    ]

    _stderr(f"spawning approver: {' '.join(cmd)}")
    _stderr(f"approver log: {log_path}")

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
    except FileNotFoundError:
        _stderr("claude CLI not found on PATH")
        raise

    assert proc.stdin is not None and proc.stdout is not None
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except OSError as e:
        _stderr(f"failed to write prompt to approver stdin: {e}")

    final_text_parts: list[str] = []
    with log_path.open("w", encoding="utf-8") as logf:
        for line in proc.stdout:
            logf.write(line)
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = _extract_assistant_text(obj)
            if text:
                final_text_parts.append(text)

    err_tail = ""
    if proc.stderr is not None:
        try:
            err_tail = proc.stderr.read() or ""
        except OSError:
            err_tail = ""
    proc.wait()
    if proc.returncode != 0:
        _stderr(f"approver exited with code {proc.returncode}; stderr tail: {err_tail[-400:]}")

    return "\n".join(final_text_parts).strip()


# ────────────────────────────────────────────────────────────────────────────
# Denial escalation to CORRECTION.md
# ────────────────────────────────────────────────────────────────────────────


def _escalate_denial(
    worksite: Path,
    task_slug: str,
    tool_name: str,
    tool_input: dict[str, Any],
    reason: str,
) -> None:
    task_dir = worksite / "simpleharness" / "tasks" / task_slug
    task_dir.mkdir(parents=True, exist_ok=True)
    path = task_dir / "CORRECTION.md"
    ts = _dt.datetime.now().isoformat(timespec="seconds")
    block = (
        f"\n## Approver denial — {ts}\n\n"
        f"Tool: {tool_name}\n"
        f"Arguments: {json.dumps(tool_input, sort_keys=True, default=str)}\n"
        f"Reason: {reason}\n\n"
        "User, do you want to override this denial? Edit this file with your "
        "instructions or delete this section to let the task continue.\n"
    )
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(block)
    except OSError as e:
        _stderr(f"failed to append to CORRECTION.md: {e}")


# ────────────────────────────────────────────────────────────────────────────
# MCP server + the review tool
# ────────────────────────────────────────────────────────────────────────────

mcp = FastMCP("simpleharness_approver")


def _missing_env_deny(var: str) -> dict[str, Any]:
    msg = f"approver not properly initialized: missing {var}"
    _stderr(msg)
    return {"behavior": "deny", "message": msg}


def _fake_verdict_from_input(tool_name: str, tool_input: dict[str, Any]) -> dict[str, str]:
    """FAKE-mode shortcut: invent an allow verdict keyed off the base command.

    Used by the integration test path so we can exercise the whole review
    pipeline without paying for a real ``claude -p`` call.
    """
    sig = _command_signature(tool_name, tool_input)
    pattern = f"{sig} *" if tool_name == "Bash" else sig
    return {
        "decision": "allow",
        "pattern": pattern,
        "reason": f"FAKE mode: auto-approved {sig}",
    }


def _do_review(tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    """Full review pipeline. Returned dict is the Claude Code permission
    response shape: ``{"behavior": "allow"|"deny", ...}``.
    """
    global _FAKE_CALL_COUNT

    fake = os.environ.get("SIMPLEHARNESS_APPROVER_FAKE") == "1"

    worksite_raw = os.environ.get("SIMPLEHARNESS_WORKSITE")
    if not worksite_raw:
        return _missing_env_deny("SIMPLEHARNESS_WORKSITE")
    worksite = Path(worksite_raw)

    task_slug = os.environ.get("SIMPLEHARNESS_TASK_SLUG")
    if not task_slug:
        return _missing_env_deny("SIMPLEHARNESS_TASK_SLUG")

    role_name = os.environ.get("SIMPLEHARNESS_ROLE", "developer")
    approver_model = os.environ.get("SIMPLEHARNESS_APPROVER_MODEL", "sonnet")
    stream_log = os.environ.get("SIMPLEHARNESS_STREAM_LOG")

    if not isinstance(tool_input, dict):
        try:
            tool_input = dict(tool_input) if tool_input is not None else {}
        except (TypeError, ValueError):
            tool_input = {}

    sig = _command_signature(tool_name, tool_input)
    probe = _tool_input_as_string(tool_name, tool_input)

    # 1) In-process cache short-circuit.
    cached_pattern = _APPROVAL_CACHE.get(sig)
    if cached_pattern and fnmatch.fnmatchcase(probe, cached_pattern):
        _stderr(f"cache hit: {sig!r} matches {cached_pattern!r}")
        return {"behavior": "allow", "updatedInput": tool_input}

    # 2) Load role + config.
    try:
        cfg = load_config(worksite)
    except Exception as e:
        _stderr(f"load_config failed: {e}")
        return {"behavior": "deny", "message": f"approver failed to load worksite config: {e}"}

    currently_approved = list(cfg.permissions.extra_bash_allow)

    # Sanity-check the role file exists and parses; we pass its path to the
    # child claude process below, but failing fast here yields a better error.
    try:
        load_role("approver")
    except Exception as e:
        _stderr(f"load_role('approver') failed: {e}")
        return {"behavior": "deny", "message": f"approver role missing or invalid: {e}"}

    # 3) Stream tail.
    stream_tail = read_stream_tail(stream_log)

    # 4) Build the prompt + spawn the approver (or FAKE).
    prompt = build_approver_prompt(
        tool_name=tool_name,
        tool_input=tool_input,
        role=role_name,
        task_slug=task_slug,
        stream_tail=stream_tail,
        currently_approved=currently_approved,
    )

    if fake:
        _FAKE_CALL_COUNT += 1
        verdict = _fake_verdict_from_input(tool_name, tool_input)
        # Still write a log so the operator audit trail is consistent.
        try:
            task_log_dir = _approver_log_dir(worksite, task_slug)
            (task_log_dir / f"approver-{_now_stamp()}-fake.jsonl").write_text(
                json.dumps(
                    {
                        "fake": True,
                        "tool_name": tool_name,
                        "tool_input": tool_input,
                        "verdict": verdict,
                        "prompt_len": len(prompt),
                    }
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError as e:
            _stderr(f"failed to write fake approver log: {e}")
    else:
        try:
            task_log_dir = _approver_log_dir(worksite, task_slug)
            final_msg = spawn_approver(
                prompt=prompt,
                approver_model=approver_model,
                approver_role_path=(toolbox_root() / "roles" / "approver.md"),
                task_log_dir=task_log_dir,
            )
        except FileNotFoundError:
            return {
                "behavior": "deny",
                "message": "approver could not be spawned: 'claude' not on PATH",
            }
        except Exception as e:
            _stderr(f"approver spawn failed: {e}")
            return {"behavior": "deny", "message": f"approver spawn failed: {e}"}
        verdict = parse_verdict(final_msg)

    # 5) Act on verdict.
    if verdict["decision"] == "allow":
        pattern = verdict["pattern"]
        try:
            append_approved_pattern(worksite, pattern)
        except Exception as e:
            _stderr(f"append_approved_pattern failed: {e}")
            # Still allow this session — the config write is best-effort — but
            # don't cache either so the next call re-tries the write.
            return {"behavior": "allow", "updatedInput": tool_input}
        _APPROVAL_CACHE[sig] = pattern
        _stderr(f"allow: sig={sig!r} pattern={pattern!r} reason={verdict['reason']!r}")
        return {"behavior": "allow", "updatedInput": tool_input}

    # deny
    reason = verdict["reason"]
    _stderr(f"deny: sig={sig!r} reason={reason!r}")
    if cfg.permissions.escalate_denials_to_correction:
        _escalate_denial(worksite, task_slug, tool_name, tool_input, reason)
    return {"behavior": "deny", "message": reason}


@mcp.tool()
async def review(tool_name: str, input: dict[str, Any]) -> dict[str, Any]:
    """Permission-prompt review tool invoked by Claude Code.

    Parameters match the ``--permission-prompt-tool`` contract: Claude Code
    passes the name of the tool it wants to run and a dict of that tool's
    arguments. The response shape is::

        {"behavior": "allow", "updatedInput": <dict>}
        # or
        {"behavior": "deny",  "message": <str>}
    """
    # Guard against unexpected input types (protocol-level defense).
    if not isinstance(tool_name, str) or not tool_name:
        return {"behavior": "deny", "message": "approver received empty tool_name"}
    tool_input: dict[str, Any] = input if isinstance(input, dict) else {}
    return _do_review(tool_name, tool_input)


def main() -> None:
    """Entry point: run the MCP server over stdio."""
    _stderr("starting SimpleHarness approver MCP server (stdio)")
    mcp.run()


if __name__ == "__main__":
    main()
