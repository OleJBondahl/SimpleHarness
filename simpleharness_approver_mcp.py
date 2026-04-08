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

import contextlib
import datetime as _dt
import fnmatch
import json
import os
import queue
import re
import shlex
import subprocess
import sys
import threading
import time
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

# In-process approval cache for *Bash* tool calls. Key: normalized command
# signature (first non-wrapper token). Value: the glob pattern that was
# approved earlier in this session. Lookup uses fnmatch against the full
# command string. Lives only for the lifetime of this MCP server process.
_APPROVAL_CACHE: dict[str, str] = {}

# In-process approval cache for *non-Bash* tool calls. Keyed on the tool name
# alone — once the approver allows e.g. WebFetch for this worksite session,
# all subsequent WebFetch calls in the same session short-circuit. This is
# more aggressive than per-input caching but matches how the approver role
# typically reasons about non-Bash tools ("WebFetch is/isn't ok in this
# worksite"). The previous implementation kept everything in `_APPROVAL_CACHE`
# but used `fnmatch.fnmatchcase(json_blob, "WebFetch")` for the lookup, which
# never matched — so non-Bash cache hits were dead code.
_NONBASH_APPROVAL_CACHE: dict[str, str] = {}

# Counter incremented each time FAKE mode short-circuits to the real approver
# path, used by the verification script to assert cache short-circuits work.
_FAKE_CALL_COUNT = 0

# Wrapper commands that prefix the *real* command we care about. Without
# unwrapping these, `_command_signature` would key e.g. `sudo apt-get update`
# under `"sudo"` — and a stored allow pattern like `sudo apt-get *` would then
# short-circuit a future `sudo rm -rf /` request via fnmatch on the same key.
# Recursing past the wrapper is a defense-in-depth (the second line of defense
# is the approver role itself never returning broad wrapper patterns).
_WRAPPER_COMMANDS: frozenset[str] = frozenset(
    {"sudo", "doas", "env", "time", "nice", "ionice", "xargs", "nohup", "timeout"}
)

# Wrapper-flag tokens we skip while drilling past a wrapper. KEY=VAL env
# assignments and short/long flags are both consumed.
_WRAPPER_FLAG_RE = re.compile(r"^-")
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Wrapper flags whose *next* token is the flag's value, not the underlying
# command. E.g. `sudo -u root apt-get` — without skipping `root` we'd treat
# it as the unwrapped command. Conservative: only the common short forms.
_WRAPPER_FLAGS_WITH_VALUE: frozenset[str] = frozenset(
    {"-u", "-g", "-h", "-p", "-D", "-C", "-T", "-r", "-t", "--user", "--group"}
)


class ApproverTimeout(Exception):
    """Raised when the spawned ``claude -p`` approver child exceeds its
    wall-clock deadline. Caught by `_do_review` and converted to a synthetic
    deny verdict so the working agent never blocks indefinitely."""


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────


def _stderr(msg: str) -> None:
    """Write a diagnostic line to stderr. Stdout is reserved for MCP traffic."""
    print(f"[approver-mcp] {msg}", file=sys.stderr, flush=True)


def _now_stamp() -> str:
    # Microsecond precision so two blocked tool calls in the same wall-clock
    # second don't overwrite each other's per-invocation log file.
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def _unwrap_wrappers(parts: list[str]) -> list[str]:
    """Drill past leading wrapper commands like ``sudo`` / ``env X=1`` and
    return the slice starting at the *real* command.

    Each iteration: if the head token is in `_WRAPPER_COMMANDS`, drop it then
    drop any wrapper-flag-like tokens (``-u root``, ``--preserve-env``, ``X=1``)
    that follow. Stop when the head is a real command, or when nothing is left.
    """
    cur = parts[:]
    # Bound the recursion so a pathological `sudo sudo sudo ...` can't loop.
    for _ in range(8):
        if not cur:
            return cur
        head = cur[0]
        if head not in _WRAPPER_COMMANDS:
            return cur
        cur = cur[1:]
        # Skip wrapper flags + env assignments. Flags in
        # _WRAPPER_FLAGS_WITH_VALUE additionally consume the next token (the
        # flag's value, not the underlying command).
        while cur:
            tok = cur[0]
            if tok in _WRAPPER_FLAGS_WITH_VALUE:
                # Drop the flag and its value (if present).
                cur = cur[2:] if len(cur) >= 2 else []
                continue
            if _WRAPPER_FLAG_RE.match(tok) or _ENV_ASSIGN_RE.match(tok):
                cur = cur[1:]
                continue
            break
    return cur


def _command_signature(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Normalize a tool call to a short cache key.

    For Bash calls, the key is the first whitespace-separated token of the
    command after stripping any leading wrapper commands (sudo, env, time,
    nice, xargs, nohup, timeout, ...). For every other tool, the key is the
    tool name itself — one approval per non-Bash tool name per session is
    plenty.

    Defense-in-depth note: Bash cache hits are gated by `fnmatch.fnmatchcase`
    against the *stored* glob pattern, so as long as the approver role never
    returns a broad wrapper pattern (`sudo *`), this is safe even when an
    attacker controls the command. Drilling past the wrapper here protects us
    from cache-key collisions if the approver ever does return such a pattern.
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
        if not parts:
            return "Bash"
        unwrapped = _unwrap_wrappers(parts)
        if unwrapped:
            return unwrapped[0]
        # Wrapper with no subcommand (e.g. bare `sudo`): fall back to the raw
        # wrapper name so we still get *some* cache key.
        return parts[0]
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


# Sentinel pushed onto the stdout queue when the reader thread reaches EOF.
_STDOUT_EOF = object()


def _spawn_pipe_reader(
    stream: Any,
    out_queue: queue.Queue[Any],
) -> threading.Thread:
    """Drain a Popen pipe line-by-line into a queue on a daemon thread."""

    def _reader() -> None:
        try:
            for line in stream:
                out_queue.put(line)
        except (OSError, ValueError):
            pass
        finally:
            out_queue.put(_STDOUT_EOF)
            with contextlib.suppress(Exception):
                stream.close()

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    return t


def _spawn_stderr_drain(stream: Any, tail: list[str], cap: int = 100) -> threading.Thread:
    """Drain stderr line-by-line into a bounded list on a daemon thread.

    Without this, a chatty child can block on a full ~64KB Windows pipe buffer
    while we're still reading stdout, deadlocking the whole call.
    """

    def _reader() -> None:
        try:
            for line in stream:
                tail.append(line)
                if len(tail) > cap:
                    del tail[: len(tail) - cap]
        except (OSError, ValueError):
            pass
        finally:
            with contextlib.suppress(Exception):
                stream.close()

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    return t


def spawn_approver(
    prompt: str,
    approver_model: str,
    approver_role_path: Path,
    task_log_dir: Path,
    worksite: Path,
    timeout: float = 120.0,
) -> str:
    """Run ``claude -p`` with the approver role appended as system prompt,
    stream its JSON events to a per-invocation log file, and return the final
    assistant text as a plain string.

    The entire subprocess lifetime is bounded by ``timeout`` seconds. On
    deadline expiry the child is killed and `ApproverTimeout` is raised so the
    working agent's blocked tool call returns a synthetic deny instead of
    hanging indefinitely.

    ``cwd`` is pinned to the worksite (NOT the parent's cwd) so the approver
    child loads the worksite's stream-log relative paths but does *not*
    inherit the working agent's project context. The toolbox root is added
    via ``--add-dir`` so the approver can read its role file. We deliberately
    do NOT pass ``--permission-prompt-tool`` here — that would let the
    approver recursively call itself.

    Note: Claude Code may still auto-load a project ``CLAUDE.md`` from the
    worksite tree. The approver role body is appended to the system prompt
    and should dominate any project-level instructions.
    """
    log_path = task_log_dir / f"approver-{_now_stamp()}.jsonl"

    tb_root = toolbox_root()

    cmd = [
        "claude",
        "-p",
        "--append-system-prompt-file",
        str(approver_role_path),
        "--add-dir",
        str(tb_root),
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
    _stderr(f"approver cwd: {worksite}")
    _stderr(f"approver timeout: {timeout:g}s")

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(worksite),
        )
    except FileNotFoundError:
        _stderr("claude CLI not found on PATH")
        raise

    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None

    # Start drain threads BEFORE writing the prompt — if the child immediately
    # bursts stderr, we don't want to deadlock between write/read.
    stdout_q: queue.Queue[Any] = queue.Queue()
    stderr_tail: list[str] = []
    _spawn_pipe_reader(proc.stdout, stdout_q)
    stderr_thread = _spawn_stderr_drain(proc.stderr, stderr_tail)

    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except OSError as e:
        _stderr(f"failed to write prompt to approver stdin: {e}")

    deadline = time.monotonic() + timeout
    final_text_parts: list[str] = []
    timed_out = False

    try:
        with log_path.open("w", encoding="utf-8") as logf:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    item = stdout_q.get(timeout=remaining)
                except queue.Empty:
                    timed_out = True
                    break
                if item is _STDOUT_EOF:
                    break
                logf.write(item)
                stripped = item.rstrip("\n")
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                text = _extract_assistant_text(obj)
                if text:
                    final_text_parts.append(text)
    finally:
        if timed_out:
            with contextlib.suppress(OSError):
                proc.kill()
            # Drain whatever stdout managed to land before kill, briefly.
            drain_deadline = time.monotonic() + 0.5
            while time.monotonic() < drain_deadline:
                try:
                    item = stdout_q.get(timeout=0.05)
                except queue.Empty:
                    continue
                if item is _STDOUT_EOF:
                    break
        # Wait the process — bounded so we don't re-introduce a hang.
        try:
            wait_remaining = max(0.5, deadline - time.monotonic())
            proc.wait(timeout=wait_remaining)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=1.0)
        # Brief join on the stderr thread so its tail is settled.
        stderr_thread.join(timeout=0.5)

    err_tail = "".join(stderr_tail)[-2000:]

    if timed_out:
        _stderr(f"approver timed out after {timeout:g}s; stderr tail: {err_tail[-400:]}")
        raise ApproverTimeout(f"approver timed out after {timeout:g}s")

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

    # Wall-clock budget for the spawned approver child. Bad values silently
    # fall back to the default so an operator typo can't unbound the deadline.
    timeout = 120.0
    timeout_raw = os.environ.get("SIMPLEHARNESS_APPROVER_TIMEOUT")
    if timeout_raw:
        try:
            parsed = float(timeout_raw)
            if parsed > 0:
                timeout = parsed
        except ValueError:
            pass

    if not isinstance(tool_input, dict):
        try:
            tool_input = dict(tool_input) if tool_input is not None else {}
        except (TypeError, ValueError):
            tool_input = {}

    sig = _command_signature(tool_name, tool_input)

    # 1) In-process cache short-circuit. Bash uses fnmatch against the stored
    # glob pattern (so `scc *` matches `scc --json .`). Non-Bash tools cache
    # at the tool-name level — see _NONBASH_APPROVAL_CACHE comment.
    if tool_name == "Bash":
        probe = _tool_input_as_string(tool_name, tool_input)
        cached_pattern = _APPROVAL_CACHE.get(sig)
        if cached_pattern and fnmatch.fnmatchcase(probe, cached_pattern):
            _stderr(f"cache hit: {sig!r} matches {cached_pattern!r}")
            return {"behavior": "allow", "updatedInput": tool_input}
    else:
        cached_pattern = _NONBASH_APPROVAL_CACHE.get(tool_name)
        if cached_pattern:
            _stderr(f"non-bash cache hit: {tool_name!r} -> {cached_pattern!r}")
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
                worksite=worksite,
                timeout=timeout,
            )
        except FileNotFoundError:
            return {
                "behavior": "deny",
                "message": "approver could not be spawned: 'claude' not on PATH",
            }
        except ApproverTimeout:
            reason = f"approver timed out after {timeout:g}s"
            _stderr(reason)
            if cfg.permissions.escalate_denials_to_correction:
                _escalate_denial(worksite, task_slug, tool_name, tool_input, reason)
            return {"behavior": "deny", "message": reason}
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
        if tool_name == "Bash":
            _APPROVAL_CACHE[sig] = pattern
        else:
            _NONBASH_APPROVAL_CACHE[tool_name] = pattern
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
