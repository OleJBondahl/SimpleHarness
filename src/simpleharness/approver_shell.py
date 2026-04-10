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
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from simpleharness.approver_core import (
    _SLUG_RE,
    ApproverEnv,
    Verdict,
    _deny_synthetic,
    _extract_assistant_text,
    _task_dir,
    finalize_review,
    parse_verdict,
    plan_review,
)
from simpleharness.core import toolbox_root
from simpleharness.io import (
    load_config,
    load_role,
    persist_approver_allow,
)

# ────────────────────────────────────────────────────────────────────────────
# Shell — I/O, subprocess, env
# ────────────────────────────────────────────────────────────────────────────


class ApproverTimeoutError(Exception):
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


def _read_stream_tail(log_path: Path | None, n_lines: int = 30) -> str:
    """Return the last ``n_lines`` assistant-text lines from the working agent's .jsonl stream log.

    Defensive on all I/O failures.
    """
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
    """Drain stderr into a bounded list on a daemon thread.

    Prevents pipe deadlock from a chatty child on Windows' ~64KB pipe buffer.
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

    assert proc.stdin is not None
    assert proc.stdout is not None
    assert proc.stderr is not None

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
        raise ApproverTimeoutError(f"approver timed out after {timeout_s:g}s")

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


def _review(env: ApproverEnv, tool_name: str, tool_input: dict[str, Any]) -> Verdict:
    """Top-level slow-path pipeline — thin coordinator.

    Gather inputs → plan (pure) → dispatch → finalize (pure) → side effects.
    """
    # ── gather ────────────────────────────────────────────────────────────────
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

    role_path = toolbox_root() / "roles" / "approver.md"
    stream_tail = _read_stream_tail(env.stream_log)

    # ── plan (pure) ───────────────────────────────────────────────────────────
    _plan = plan_review(
        env,
        tool_name,
        tool_input,
        cfg,
        role_file_exists=role_path.is_file(),
        stream_tail=stream_tail,
        currently_approved=tuple(cfg.permissions.extra_bash_allow),
    )

    # ── dispatch ──────────────────────────────────────────────────────────────
    if _plan.preemptive_deny:
        return _deny_synthetic(_plan.preemptive_deny)

    if _plan.fake_verdict is not None:
        verdict = _plan.fake_verdict
        # side effect: write fake log
        try:
            task_log_dir = _approver_log_dir(env.worksite, env.task_slug)
            (task_log_dir / f"approver-{_now_stamp()}-fake.jsonl").write_text(
                json.dumps(
                    {
                        "fake": True,
                        "tool_name": tool_name,
                        "tool_input": tool_input,
                        "verdict": dataclasses.asdict(verdict),
                        "prompt_len": len(_plan.prompt),
                    }
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError as e:
            _stderr(f"failed to write fake approver log: {e}")
    else:
        if _plan.spawn is None:
            return _deny_synthetic("internal error: review plan missing spawn request")
        # Shell builds the real role_path for spawn (plan only has a placeholder)
        try:
            task_log_dir = _approver_log_dir(env.worksite, env.task_slug)
            final_msg = _spawn_approver(
                prompt=_plan.spawn.prompt,
                approver_model=_plan.spawn.approver_model,
                approver_role_path=role_path,
                task_log_dir=task_log_dir,
                worksite=_plan.spawn.worksite,
                timeout_s=_plan.spawn.timeout_s,
            )
        except FileNotFoundError:
            return _deny_synthetic("approver could not be spawned: 'claude' not on PATH")
        except ApproverTimeoutError:
            reason = f"approver timed out after {env.timeout_s:g}s"
            _stderr(reason)
            if cfg.permissions.escalate_denials_to_correction:
                _escalate_denial(env.worksite, env.task_slug, tool_name, tool_input, reason)
            return _deny_synthetic(reason)
        except Exception as e:
            _stderr(f"approver spawn failed: {e}")
            return _deny_synthetic(f"approver spawn failed: {e}")
        verdict = parse_verdict(final_msg)

    # ── finalize (pure) + side effects ────────────────────────────────────────
    outcome = finalize_review(verdict, cfg)

    if outcome.pattern_to_persist:
        try:
            persist_approver_allow(
                env.worksite,
                outcome.pattern_to_persist,
                _task_dir(env.worksite, env.task_slug),
            )
        except Exception as e:
            _stderr(f"persist_approver_allow failed: {e}")
        _stderr(f"allow: pattern={outcome.pattern_to_persist!r} reason={verdict.reason!r}")

    if outcome.should_escalate:
        _escalate_denial(env.worksite, env.task_slug, tool_name, tool_input, verdict.reason)

    if verdict.decision == "deny":
        _stderr(f"deny: reason={verdict.reason!r}")

    return outcome.verdict


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
