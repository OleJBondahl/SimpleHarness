"""Session spawning, streaming, pretty-printing, SIGINT handling, and main runner."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import uuid
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from rich.panel import Panel
from rich.text import Text

from simpleharness.core import (
    DEFAULT_BASH_ALLOW,
    Config,
    Role,
    SessionResult,
    SkillList,
    Task,
    Workflow,
    _format_tool_call,
    build_claude_cmd,
    build_exported_subagent_file,
    build_session_env,
    build_session_prompt,
    merge_skill_lists,
    toolbox_root,
    worksite_sh_dir,
)
from simpleharness.io import (
    _write_approver_settings,
    _write_session_settings,
    consume_correction,
    list_phase_files,
    load_all_subagents,
    write_approver_allowlist,
    write_session_prompt_file,
)
from simpleharness.ui import console, say, warn

if TYPE_CHECKING:
    from pathlib import Path


def _popen_kwargs_windows() -> dict[str, Any]:
    """Windows-specific: CREATE_NEW_PROCESS_GROUP so Ctrl+C stays in the parent."""
    if sys.platform != "win32":
        return {}
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}


def spawn_claude(
    cmd: list[str],
    cwd: Path,
    *,
    extra_env: dict[str, str] | None = None,
) -> subprocess.Popen[str]:
    """Spawn a Claude Code subprocess and return the Popen handle."""
    env: dict[str, str] | None = None
    if extra_env:
        env = os.environ.copy()
        env.update(extra_env)
    return subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
        **_popen_kwargs_windows(),
    )


def terminate_child(proc: subprocess.Popen[str]) -> None:
    """Best-effort kill of a child process. Windows-safe."""
    if proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
    except (ProcessLookupError, OSError):
        pass


def _pretty_event(event: dict[str, Any]) -> None:
    """Render a single stream-json event to the terminal using rich."""
    etype = event.get("type", "")
    if etype == "stream_event":
        return
    if etype == "system":
        sub = event.get("subtype", "")
        if sub == "init":
            tools = event.get("tools", [])
            console.print(
                Panel.fit(
                    Text(f"session init  |  tools: {len(tools)}", style="dim"),
                    border_style="cyan",
                )
            )
        return
    if etype == "assistant":
        msg = event.get("message", {})
        for block in msg.get("content", []) or []:
            btype = block.get("type", "")
            if btype == "text":
                text = block.get("text", "")
                if text.strip():
                    console.print(text, markup=False, highlight=False)
            elif btype == "tool_use":
                tname = block.get("name", "?")
                tinput = block.get("input", {}) or {}
                pretty = _format_tool_call(tname, tinput)
                from rich.markup import escape as _rich_escape

                console.print(rf"[magenta]\u2192 {tname}[/] [dim]{_rich_escape(pretty)}[/]")
            elif btype == "thinking":
                console.print(r"[dim italic]  \[thinking\.\.\.][/]")
        return
    if etype == "user":
        msg = event.get("message", {})
        for block in msg.get("content", []) or []:
            if block.get("type") == "tool_result":
                content = block.get("content", "")
                is_error = bool(block.get("is_error"))
                if isinstance(content, list):
                    content = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
                text = str(content)
                from rich.markup import escape as _rich_escape

                if is_error:
                    preview = text[:600]
                    suffix = f" [dim](+{len(text) - 600} more)[/]" if len(text) > 600 else ""
                    console.print(
                        rf"[red]  \u2717 error[/] [dim]{_rich_escape(preview)}[/]{suffix}"
                    )
                else:
                    preview = text[:800]
                    suffix = f" [dim](+{len(text) - 800} more)[/]" if len(text) > 800 else ""
                    console.print(
                        rf"[green]  \u2190 result[/] [dim]{_rich_escape(preview)}[/]{suffix}"
                    )
        return
    if etype == "result":
        status = "ok" if not event.get("is_error") else "ERROR"
        duration = event.get("duration_ms", 0)
        cost = event.get("total_cost_usd")
        cost_str = f" ${cost:.4f}" if isinstance(cost, (int, float)) else ""
        console.print(
            Panel.fit(
                Text(
                    f"session result: {status}  |  {duration} ms{cost_str}",
                    style="bold",
                ),
                border_style="green" if status == "ok" else "red",
            )
        )
        return
    console.print(rf"[dim]  \[{etype}][/]")


def stream_and_log(
    proc: subprocess.Popen[str],
    jsonl_log: Path,
    plain_log: Path,
    *,
    model: str = "",
    provider: str = "",
) -> tuple[str | None, str | None, float | None, int | None]:
    """Read proc.stdout line-by-line, pretty-print + log, return (session_id, result_text, cost_usd, duration_ms)."""
    session_id: str | None = None
    result_text: str | None = None
    total_cost: float | None = None
    total_duration: int | None = None
    jsonl_log.parent.mkdir(parents=True, exist_ok=True)
    with (
        jsonl_log.open("w", encoding="utf-8") as jf,
        plain_log.open("w", encoding="utf-8") as pf,
    ):
        if model:
            jf.write(
                json.dumps({"type": "session_meta", "model": model, "provider": provider}) + "\n"
            )
            jf.flush()
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            if not line.strip():
                continue
            jf.write(line + "\n")
            jf.flush()
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                pf.write(line + "\n")
                pf.flush()
                from rich.markup import escape as _rich_escape

                console.print(rf"[dim red]  \[non-json][/] {_rich_escape(line[:200])}")
                continue
            if isinstance(event, dict):
                if event.get("type") == "system" and event.get("subtype") == "init":
                    session_id = event.get("session_id") or session_id
                elif event.get("type") == "result":
                    session_id = event.get("session_id") or session_id
                    result_text = event.get("result")
                    cost = event.get("total_cost_usd")
                    if isinstance(cost, (int, float)):
                        total_cost = float(cost)
                    duration = event.get("duration_ms")
                    if isinstance(duration, int):
                        total_duration = duration
            if isinstance(event, dict):
                etype = event.get("type")
                if etype == "assistant":
                    for block in event.get("message", {}).get("content", []) or []:
                        btype = block.get("type")
                        if btype == "text":
                            pf.write(block.get("text", "") + "\n")
                        elif btype == "tool_use":
                            tname = block.get("name", "?")
                            tinput = block.get("input", {}) or {}
                            pf.write(f"\u2192 {tname} {_format_tool_call(tname, tinput)}\n")
                elif etype == "user":
                    for block in event.get("message", {}).get("content", []) or []:
                        if block.get("type") == "tool_result":
                            content = block.get("content", "")
                            if isinstance(content, list):
                                content = "\n".join(
                                    c.get("text", "") for c in content if isinstance(c, dict)
                                )
                            marker = "\u2717" if block.get("is_error") else "\u2190"
                            pf.write(f"  {marker} {str(content)[:800]}\n")
            pf.flush()
            _pretty_event(event if isinstance(event, dict) else {"type": "unknown"})
    return session_id, result_text, total_cost, total_duration


class InterventionState:
    """Mutable flag a signal handler can flip without raising mid-read."""

    abort: bool = False


_intervention = InterventionState()


def read_stdin_until_blank() -> list[str]:
    """Read lines from stdin until a blank line or second Ctrl+C."""
    lines: list[str] = []
    console.print(
        "\n[yellow]\\[harness][/] Type your correction. "
        "Blank line + Enter = save & resume. Ctrl+C again = abort.\n",
    )
    try:
        while True:
            try:
                line = input()
            except EOFError:
                break
            if line.strip() == "":
                break
            lines.append(line)
    except KeyboardInterrupt:
        _intervention.abort = True
    return lines


def write_correction_md(task: Task, lines: list[str]) -> Path:
    """Write a CORRECTION.md file to the task folder and return its path."""
    path = task.folder / "CORRECTION.md"
    body = "\n".join(lines).strip()
    path.write_text(body + "\n", encoding="utf-8")
    return path


def run_session(
    task: Task, role: Role, workflow: Workflow, config: Config, worksite: Path
) -> SessionResult:
    """Build prompt, spawn claude, stream output, handle SIGINT. Single session."""
    toolbox = toolbox_root()

    # 1. consume correction file if present
    correction = consume_correction(task)

    # 2. build and write prompt
    phase_files = list_phase_files(task.folder)
    # Read first 20 lines of each phase file for context in the prompt
    phase_previews: dict[str, str] = {}
    for pf in phase_files:
        try:
            text = pf.read_text(encoding="utf-8")
            preview_lines = text.splitlines()[:20]
            if len(text.splitlines()) > 20:
                preview_lines.append("... (truncated)")
            phase_previews[pf.name] = "\n".join(preview_lines)
        except OSError:
            pass
    # Read WORKSITE.md cross-session memory for prompt preview
    worksite_memory_preview: str | None = None
    memory_path = worksite_sh_dir(worksite) / "memory" / "WORKSITE.md"
    if memory_path.exists():
        try:
            raw = memory_path.read_text(encoding="utf-8")
            raw_lines = raw.splitlines()
            # Skip if it's just the template header with no real content
            content_lines = [ln for ln in raw_lines if ln.strip() and not ln.startswith("# ")]
            if content_lines:
                preview_lines = raw_lines[:20]
                if len(raw_lines) > 20:
                    preview_lines.append("... (truncated)")
                worksite_memory_preview = "\n".join(preview_lines)
        except OSError:
            pass

    prompt = build_session_prompt(
        task,
        role,
        workflow,
        toolbox,
        correction,
        phase_files,
        phase_previews,
        worksite_memory_preview=worksite_memory_preview,
        worksite=worksite,
    )
    prompt_file = write_session_prompt_file(task, prompt)

    # 3. log paths
    log_root = worksite_sh_dir(worksite) / "logs" / task.slug
    idx = task.state.total_sessions
    stem = f"{idx:02d}-{role.name}"
    jsonl_log = log_root / f"{stem}.jsonl"
    plain_log = log_root / f"{stem}.log"

    # 4. export subagents to <worksite>/.claude/agents/
    subagents = load_all_subagents()
    if subagents:
        agents_dir = worksite / ".claude" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        defaults = SkillList(
            available=config.skills.default_available,
            must_use=config.skills.default_must_use,
        )
        for sa in subagents:
            merged_sa = merge_skill_lists(sa.skills, defaults)
            exported_sa = replace(sa, skills=merged_sa)
            content = build_exported_subagent_file(exported_sa)
            (agents_dir / f"{sa.name}.md").write_text(content, encoding="utf-8")

    # 5. build command
    session_id = str(uuid.uuid4())
    enforcement_mode = config.skills.enforcement
    if config.permissions.mode == "approver":
        bash_patterns = list(DEFAULT_BASH_ALLOW) + list(config.permissions.extra_bash_allow)
        write_approver_allowlist(task.folder, bash_patterns)
        approver_settings_path: Path | None = _write_approver_settings(
            task.folder, enforcement_mode
        )
    else:
        approver_settings_path = _write_session_settings(
            task.folder,
            enforcement_mode,
            is_local=(role.provider == "ollama"),
        )
    cmd = build_claude_cmd(
        prompt_file,
        role,
        toolbox,
        session_id,
        config,
        approver_settings_path=approver_settings_path,
    )

    # 6. banner
    console.rule(f"[cyan]session {idx + 1}  [bold]{role.name}[/]  task={task.slug}")
    resolved_model = role.model or config.model
    resolved_provider = role.provider or "subscription"
    say(
        f"model={resolved_model}  provider={resolved_provider}  session_id={session_id[:8]}  max_turns={role.max_turns or config.max_turns_default}"
    )
    if correction:
        say(
            "CORRECTION.md was consumed and injected into this session's prompt.",
            style="yellow",
        )

    # 7. env exports
    approver_base: dict[str, str] = {}
    if config.permissions.mode == "approver":
        approver_base = {
            "SIMPLEHARNESS_STREAM_LOG": jsonl_log.as_posix(),
            "SIMPLEHARNESS_WORKSITE": worksite.as_posix(),
            "SIMPLEHARNESS_APPROVER_MODEL": config.permissions.approver_model,
            "SIMPLEHARNESS_TASK_SLUG": task.slug,
        }
    extra_env: dict[str, str] = build_session_env(approver_base, role, subagents, config)

    # 8. spawn + stream
    proc = spawn_claude(cmd, worksite, extra_env=extra_env)
    interrupted = False
    result_session_id: str | None = None
    result_text: str | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None
    try:
        result_session_id, result_text, cost_usd, duration_ms = stream_and_log(
            proc,
            jsonl_log,
            plain_log,
            model=resolved_model,
            provider=resolved_provider,
        )
        proc.wait()
    except KeyboardInterrupt:
        interrupted = True
        warn("session interrupted by user (Ctrl+C)")
        terminate_child(proc)
        _intervention.abort = False
        lines = read_stdin_until_blank()
        if lines:
            cpath = write_correction_md(task, lines)
            say(f"CORRECTION.md saved to {cpath}")
        else:
            say("no correction text entered")
        if _intervention.abort:
            say("second Ctrl+C detected \u2014 aborting harness")
            raise
    finally:
        if proc.poll() is None:
            terminate_child(proc)

    exit_code = proc.returncode
    completed = not interrupted and exit_code == 0
    return SessionResult(
        completed=completed,
        interrupted=interrupted,
        session_id=result_session_id or session_id,
        result_text=result_text,
        exit_code=exit_code,
        cost_usd=cost_usd,
        duration_ms=duration_ms,
    )
