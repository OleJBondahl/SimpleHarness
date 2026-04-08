"""simpleharness approver mode — PreToolUse hook, Python slow path.

Invoked by ``simpleharness_approver_hook.sh`` when a Bash command misses
the static allowlist fast path. Spawns a ``claude -p`` approver session
with the approver role body, parses the verdict JSON from the final
assistant message, and on allow refreshes ``.approver-allowlist.txt`` so
future calls in the same session hit the fast path.

stdin:  Claude Code PreToolUse envelope {tool_name, tool_input, ...}
stdout: one hookSpecificOutput JSON object
stderr: diagnostics and audit trail

Style: functional core, imperative shell. Pure helpers (``parse_verdict``,
``command_signature``, ``unwrap_wrappers``, ``build_approver_prompt``)
take explicit inputs and return values with no I/O; shell functions
(``main``, ``_load_env``, ``_read_stream_tail``, ``_spawn_approver``,
``_emit``) own stdin/stdout/env/subprocess/file access.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as _dt
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

from simpleharness_core import (
    load_config,
    load_role,
    persist_approver_allow,
    toolbox_root,
)

# ────────────────────────────────────────────────────────────────────────────
# Core — pure functions, no I/O
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


@dataclasses.dataclass(frozen=True)
class ApproverEnv:
    worksite: Path
    task_slug: str
    role: str
    approver_model: str
    stream_log: Path | None
    fake: bool
    timeout_s: float


@dataclasses.dataclass(frozen=True)
class Verdict:
    decision: str  # "allow" | "deny"
    pattern: str  # "" on deny
    reason: str


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
        parts = shlex.split(raw, posix=(os.name != "nt"))
    except ValueError:
        parts = raw.split()
    if not parts:
        return "Bash"
    unwrapped = unwrap_wrappers(parts)
    if unwrapped:
        return unwrapped[0]
    return parts[0]


# Last fenced JSON code block wins — models sometimes show a draft in
# an earlier block then commit to a final one at the bottom.
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL | re.IGNORECASE)


def _deny_synthetic(reason: str) -> Verdict:
    return Verdict(decision="deny", pattern="", reason=reason)


def parse_verdict(final_message: str) -> Verdict:
    """Extract and validate the approver's JSON verdict.

    On any parse failure (no block, bad JSON, missing fields, invalid
    decision enum, non-string reason), returns a synthetic deny Verdict
    with a diagnostic reason. Never raises.
    """
    if not isinstance(final_message, str) or not final_message.strip():
        return _deny_synthetic("approver returned empty message")

    matches = _JSON_BLOCK_RE.findall(final_message)
    if not matches:
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

    return Verdict(decision=decision, pattern=pattern, reason=reason)


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


# ────────────────────────────────────────────────────────────────────────────
# Shell — I/O, subprocess, env
# ────────────────────────────────────────────────────────────────────────────


class ApproverTimeout(Exception):
    """Raised when the spawned ``claude -p`` child exceeds its wall-clock deadline."""


def _stderr(msg: str) -> None:
    print(f"[approver-hook] {msg}", file=sys.stderr, flush=True)


def _emit(decision: str, reason: str) -> None:
    """Write the single hook response JSON to stdout."""
    response = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision if decision in ("allow", "deny") else "deny",
            "permissionDecisionReason": reason,
        }
    }
    sys.stdout.write(json.dumps(response))
    sys.stdout.flush()


def _now_stamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def _load_env() -> ApproverEnv | str:
    """Read env vars, return ApproverEnv on success or an error string on failure."""
    worksite_raw = os.environ.get("SIMPLEHARNESS_WORKSITE")
    if not worksite_raw:
        return "approver not properly initialized: missing SIMPLEHARNESS_WORKSITE"
    task_slug = os.environ.get("SIMPLEHARNESS_TASK_SLUG")
    if not task_slug:
        return "approver not properly initialized: missing SIMPLEHARNESS_TASK_SLUG"
    if not _SLUG_RE.match(task_slug):
        return f"invalid SIMPLEHARNESS_TASK_SLUG shape: {task_slug!r}"

    role = os.environ.get("SIMPLEHARNESS_ROLE", "developer")
    approver_model = os.environ.get("SIMPLEHARNESS_APPROVER_MODEL", "sonnet")
    stream_log_raw = os.environ.get("SIMPLEHARNESS_STREAM_LOG")
    stream_log = Path(stream_log_raw) if stream_log_raw else None
    fake = os.environ.get("SIMPLEHARNESS_APPROVER_FAKE") == "1"

    timeout_s = 120.0
    t_raw = os.environ.get("SIMPLEHARNESS_APPROVER_TIMEOUT")
    if t_raw:
        try:
            parsed = float(t_raw)
            if parsed > 0:
                timeout_s = parsed
        except ValueError:
            pass

    return ApproverEnv(
        worksite=Path(worksite_raw),
        task_slug=task_slug,
        role=role,
        approver_model=approver_model,
        stream_log=stream_log,
        fake=fake,
        timeout_s=timeout_s,
    )


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


def _read_stream_tail(log_path: Path | None, n_lines: int = 30) -> str:
    """Return the last ``n_lines`` assistant-text lines from the working
    agent's .jsonl stream log. Defensive on all I/O failures."""
    if not log_path:
        return "(no stream context available)"
    try:
        if not log_path.exists() or not log_path.is_file():
            return "(no stream context available)"
        raw = log_path.read_text(encoding="utf-8", errors="replace")
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
    return "\n".join(out[-n_lines:])


_STDOUT_EOF = object()


def _spawn_pipe_reader(stream: Any, out_queue: queue.Queue[Any]) -> threading.Thread:
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
    """Drain stderr into a bounded list on a daemon thread — prevents
    pipe deadlock from a chatty child on Windows' ~64KB pipe buffer."""

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


def _approver_log_dir(worksite: Path, task_slug: str) -> Path:
    d = worksite / "simpleharness" / "logs" / task_slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def _spawn_approver(
    prompt: str,
    approver_model: str,
    approver_role_path: Path,
    task_log_dir: Path,
    worksite: Path,
    timeout_s: float,
) -> str:
    """Run ``claude -p`` as the approver, return its final assistant text.

    Bounded by ``timeout_s`` seconds wall-clock. ``cwd`` pinned to the
    worksite. ``--add-dir`` exposes the toolbox so the approver can
    read its role file. Deliberately does NOT pass ``--settings`` /
    ``--permission-prompt-tool`` so the child can't recurse into us.
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
    _stderr(f"approver timeout: {timeout_s:g}s")

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

    stdout_q: queue.Queue[Any] = queue.Queue()
    stderr_tail: list[str] = []
    _spawn_pipe_reader(proc.stdout, stdout_q)
    stderr_thread = _spawn_stderr_drain(proc.stderr, stderr_tail)

    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except OSError as e:
        _stderr(f"failed to write prompt to approver stdin: {e}")

    deadline = time.monotonic() + timeout_s
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
            drain_deadline = time.monotonic() + 0.5
            while time.monotonic() < drain_deadline:
                try:
                    item = stdout_q.get(timeout=0.05)
                except queue.Empty:
                    continue
                if item is _STDOUT_EOF:
                    break
        try:
            wait_remaining = max(0.5, deadline - time.monotonic())
            proc.wait(timeout=wait_remaining)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=1.0)
        stderr_thread.join(timeout=0.5)

    err_tail = "".join(stderr_tail)[-2000:]

    if timed_out:
        _stderr(f"approver timed out after {timeout_s:g}s; stderr tail: {err_tail[-400:]}")
        raise ApproverTimeout(f"approver timed out after {timeout_s:g}s")

    if proc.returncode != 0:
        _stderr(f"approver exited with code {proc.returncode}; stderr tail: {err_tail[-400:]}")

    return "\n".join(final_text_parts).strip()


def _escalate_denial(
    worksite: Path,
    task_slug: str,
    tool_name: str,
    tool_input: dict[str, Any],
    reason: str,
) -> None:
    """Append a structured review block to ``<task>/CORRECTION.md``."""
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


def _task_dir(worksite: Path, task_slug: str) -> Path:
    return worksite / "simpleharness" / "tasks" / task_slug


def _review(env: ApproverEnv, tool_name: str, tool_input: dict[str, Any]) -> Verdict:
    """Top-level slow-path pipeline.

    1. Load config + approver role.
    2. Read stream tail.
    3. FAKE mode: synthesize verdict; else build prompt + spawn + parse.
    4. On allow: persist pattern + refresh fast-path allowlist.
    5. On deny: maybe escalate to CORRECTION.md.
    """
    try:
        cfg = load_config(env.worksite)
    except Exception as e:
        _stderr(f"load_config failed: {e}")
        return _deny_synthetic(f"approver failed to load worksite config: {e}")

    try:
        load_role("approver")
    except Exception as e:
        _stderr(f"load_role('approver') failed: {e}")
        return _deny_synthetic(f"approver role missing or invalid: {e}")

    # Belt-and-braces: construct the exact path the spawn will pass to
    # ``--append-system-prompt-file`` and verify it exists here so we
    # never silently spawn a child with a bogus prompt file.
    role_path = toolbox_root() / "roles" / "approver.md"
    if not role_path.is_file():
        return _deny_synthetic(f"approver role file not found at {role_path}")

    stream_tail = _read_stream_tail(env.stream_log)

    prompt = build_approver_prompt(
        tool_name=tool_name,
        tool_input=tool_input,
        role=env.role,
        task_slug=env.task_slug,
        stream_tail=stream_tail,
        currently_approved=list(cfg.permissions.extra_bash_allow),
    )

    if env.fake:
        verdict = fake_verdict_from_input(tool_name, tool_input)
        try:
            task_log_dir = _approver_log_dir(env.worksite, env.task_slug)
            (task_log_dir / f"approver-{_now_stamp()}-fake.jsonl").write_text(
                json.dumps(
                    {
                        "fake": True,
                        "tool_name": tool_name,
                        "tool_input": tool_input,
                        "verdict": dataclasses.asdict(verdict),
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
            task_log_dir = _approver_log_dir(env.worksite, env.task_slug)
            final_msg = _spawn_approver(
                prompt=prompt,
                approver_model=env.approver_model,
                approver_role_path=role_path,
                task_log_dir=task_log_dir,
                worksite=env.worksite,
                timeout_s=env.timeout_s,
            )
        except FileNotFoundError:
            return _deny_synthetic("approver could not be spawned: 'claude' not on PATH")
        except ApproverTimeout:
            reason = f"approver timed out after {env.timeout_s:g}s"
            _stderr(reason)
            if cfg.permissions.escalate_denials_to_correction:
                _escalate_denial(env.worksite, env.task_slug, tool_name, tool_input, reason)
            return _deny_synthetic(reason)
        except Exception as e:
            _stderr(f"approver spawn failed: {e}")
            return _deny_synthetic(f"approver spawn failed: {e}")
        verdict = parse_verdict(final_msg)

    if verdict.decision == "allow":
        pattern = verdict.pattern
        # Persist the pattern to config.yaml AND refresh the fast-path
        # allowlist under a single shared lock so concurrent approver
        # processes cannot race and drop patterns from the allowlist
        # file. Config write is best-effort: on failure we still allow
        # the current call so the working agent isn't blocked.
        try:
            persist_approver_allow(env.worksite, pattern, _task_dir(env.worksite, env.task_slug))
        except Exception as e:
            _stderr(f"persist_approver_allow failed: {e}")
        _stderr(f"allow: pattern={pattern!r} reason={verdict.reason!r}")
        return verdict

    # deny
    _stderr(f"deny: reason={verdict.reason!r}")
    if cfg.permissions.escalate_denials_to_correction:
        _escalate_denial(env.worksite, env.task_slug, tool_name, tool_input, verdict.reason)
    return verdict


def main() -> None:
    """Thin shell entry point. Reads stdin, decodes, calls core, writes stdout.

    Wrapped in a top-level ``BaseException`` catch because this runs as
    an unattended PreToolUse hook: any unhandled exception (including
    ``KeyboardInterrupt`` and ``SystemExit``) would produce a traceback
    on stderr and empty stdout, which Claude Code sees as a malformed
    hook response. Silent graceful deny is the right default here.
    """
    try:
        try:
            raw = sys.stdin.read()
            envelope = json.loads(raw) if raw.strip() else {}
        except Exception as exc:
            _emit("deny", f"hook failed to parse stdin envelope: {exc}")
            return

        tool_name = envelope.get("tool_name") if isinstance(envelope, dict) else None
        if not isinstance(tool_name, str) or not tool_name:
            tool_name = "<unknown>"
        tool_input = envelope.get("tool_input") if isinstance(envelope, dict) else {}
        if not isinstance(tool_input, dict):
            tool_input = {}

        env = _load_env()
        if isinstance(env, str):
            _emit("deny", env)
            return

        try:
            verdict = _review(env, tool_name, tool_input)
        except ApproverTimeout as exc:
            _emit("deny", f"approver timed out: {exc}")
            return
        except Exception as exc:
            _stderr(f"hook internal error: {exc!r}")
            _emit("deny", f"approver hook internal error: {type(exc).__name__}: {exc}")
            return

        _emit(verdict.decision, verdict.reason)
    except BaseException as exc:
        # Hook must never crash — belt-and-braces suppress lets us still
        # exit 0 even if stdout itself is broken.
        with contextlib.suppress(Exception):
            _emit("deny", f"approver hook interrupted: {type(exc).__name__}: {exc}")
        return


if __name__ == "__main__":
    main()
