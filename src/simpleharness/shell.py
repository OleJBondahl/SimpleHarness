"""SimpleHarness - a lightweight baton-pass agent harness over the Claude Code CLI.

Single-file MVP. Reads markdown role and workflow definitions from the toolbox
repo, scans a worksite's simpleharness/ folder for tasks, and runs headless
`claude -p` sessions one at a time, passing state between them via STATE.md
and per-phase markdown files on disk.

See README.md and the design plan for full architecture notes.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from simpleharness.core import (
    _VALID_APPROVER_MODELS,
    _VALID_MODES,
    DEFAULT_BASH_ALLOW,
    Config,
    Role,
    SessionResult,
    State,
    Task,
    Workflow,
    _format_tool_call,
    _slugify,
    build_claude_cmd,
    build_session_prompt,
    compute_post_session_state,
    plan_tick,
    resolve_next_role,
    toolbox_root,
    worksite_sh_dir,
)
from simpleharness.core import (
    DEFAULT_TOOLS_ALLOW as DEFAULT_TOOLS_ALLOW,  # re-export
)
from simpleharness.core import (
    Permissions as Permissions,  # re-export for downstream scripts
)
from simpleharness.core import (
    _merge_config as _merge_config,  # re-export
)
from simpleharness.core import (
    parse_frontmatter as parse_frontmatter,  # re-export
)

# ────────────────────────────────────────────────────────────────────────────
# YAML frontmatter + config file helpers (impure — file I/O)
# ────────────────────────────────────────────────────────────────────────────


def read_frontmatter_file(path: Path) -> tuple[dict[str, Any], str]:
    return parse_frontmatter(path.read_text(encoding="utf-8"))


def _load_yaml_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a YAML mapping at top level")
    return data


def load_config(worksite: Path) -> Config:
    """Load toolbox config.yaml merged with per-worksite overrides."""
    toolbox_cfg = _load_yaml_file(toolbox_root() / "config.yaml")
    worksite_cfg = _load_yaml_file(worksite / "simpleharness" / "config.yaml")
    merged = _merge_config(toolbox_cfg, worksite_cfg)

    perms_raw = merged.get("permissions", {}) or {}

    mode = perms_raw.get("mode", "safe")
    if mode is None:
        mode = "safe"
    if not isinstance(mode, str) or mode not in _VALID_MODES:
        raise ValueError(f"permissions.mode: invalid value {mode!r}; must be one of {_VALID_MODES}")

    approver_model = perms_raw.get("approver_model", "sonnet")
    if approver_model is None:
        approver_model = "sonnet"
    if not isinstance(approver_model, str) or approver_model not in _VALID_APPROVER_MODELS:
        raise ValueError(
            f"permissions.approver_model: invalid value {approver_model!r}; "
            f"must be one of {_VALID_APPROVER_MODELS}"
        )

    escalate = perms_raw.get("escalate_denials_to_correction", False)
    if escalate is None:
        escalate = False
    if not isinstance(escalate, bool):
        raise ValueError(
            f"permissions.escalate_denials_to_correction: must be a bool, "
            f"got {type(escalate).__name__}"
        )

    perms = Permissions(
        mode=mode,
        approver_model=approver_model,
        escalate_denials_to_correction=escalate,
        extra_bash_allow=tuple(perms_raw.get("extra_bash_allow", []) or []),
        extra_tools_allow=tuple(perms_raw.get("extra_tools_allow", []) or []),
    )
    return Config(
        model=str(merged.get("model", "opus")),
        idle_sleep_seconds=int(merged.get("idle_sleep_seconds", 30)),
        max_sessions_per_task=int(merged.get("max_sessions_per_task", 20)),
        max_same_role_repeats=int(merged.get("max_same_role_repeats", 3)),
        no_progress_tick_threshold=int(merged.get("no_progress_tick_threshold", 5)),
        max_turns_default=int(merged.get("max_turns_default", 60)),
        include_partial_messages=bool(merged.get("include_partial_messages", True)),
        permissions=perms,
    )


def load_role(name: str) -> Role:
    path = toolbox_root() / "roles" / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"role '{name}' not found at {path}")
    meta, body = read_frontmatter_file(path)
    return Role(
        name=str(meta.get("name", name)),
        body=body.strip(),
        description=str(meta.get("description", "")),
        model=meta.get("model"),
        max_turns=meta.get("max_turns"),
        allowed_tools=tuple(meta.get("allowed_tools", []) or []),
        privileged=bool(meta.get("privileged", False)),
        source_path=path,
    )


# ────────────────────────────────────────────────────────────────────────────
# Process management + file locking (impure — os, time)
# ────────────────────────────────────────────────────────────────────────────


def _pid_alive(pid: int) -> bool:
    """Return True if `pid` refers to a running process.

    Conservative: on any unexpected failure, returns True (treat as alive)
    so we don't accidentally steal a live lock.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        SYNCHRONIZE = 0x00100000
        ERROR_INVALID_PARAMETER = 87
        try:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            err = ctypes.get_last_error() or kernel32.GetLastError()
            # ERROR_INVALID_PARAMETER -> dead; anything else (e.g. access
            # denied) -> assume alive-but-foreign.
            return err != ERROR_INVALID_PARAMETER
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


class _FileLock:
    """Cross-platform exclusive lock via sibling .lock file.

    Uses os.open with O_CREAT | O_EXCL, which is atomic on both Windows and
    POSIX filesystems. Spins with short sleeps until acquired. Writes the
    holder's PID into the lockfile so stale locks from crashed holders can
    be reclaimed on the next acquisition attempt.
    """

    def __init__(self, target: Path, timeout: float = 10.0, poll: float = 0.05) -> None:
        self.lock_path = target.with_suffix(target.suffix + ".lock")
        self.timeout = timeout
        self.poll = poll
        self._fd: int | None = None

    def __enter__(self) -> _FileLock:
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fd = os.open(
                    str(self.lock_path),
                    os.O_CREAT | os.O_EXCL | os.O_RDWR,
                )
            except FileExistsError:
                # Inspect the existing lockfile to see if the holder is dead.
                reclaimed = False
                try:
                    with open(self.lock_path, "rb") as f:
                        raw = f.read().strip()
                    if raw:
                        try:
                            other_pid = int(raw)
                        except ValueError:
                            other_pid = -1
                        if other_pid > 0 and not _pid_alive(other_pid):
                            with contextlib.suppress(FileNotFoundError):
                                os.unlink(self.lock_path)
                            reclaimed = True
                    # Empty/unreadable PID: be conservative, treat as alive.
                except FileNotFoundError:
                    # Lock disappeared between the two calls; retry immediately.
                    reclaimed = True
                except OSError:
                    # Any other read error: be conservative, treat as alive.
                    pass
                if reclaimed:
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"could not acquire lock {self.lock_path} within {self.timeout}s"
                    ) from None
                time.sleep(self.poll)
                continue
            # Acquired: stamp our PID into the lockfile contents so a future
            # caller can detect and reclaim the file if this process dies.
            # Non-fatal on OSError: the lock still exists; reclaim path just
            # won't be able to read a PID and will conservatively spin.
            with contextlib.suppress(OSError):
                os.write(fd, str(os.getpid()).encode("ascii"))
            self._fd = fd
            return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        # NOTE: On Windows the fd MUST be closed before os.unlink, otherwise
        # the unlink fails with a sharing violation. Preserve this ordering.
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.lock_path)


# ────────────────────────────────────────────────────────────────────────────
# Approver allowlist writers (impure — file I/O)
# ────────────────────────────────────────────────────────────────────────────


_ALLOWLIST_HEADER = (
    "# simpleharness approver fast-path allowlist\n"
    "# Regenerated by the harness on session spawn and by the Python slow\n"
    "# path after an approver allow verdict. Do not hand-edit — your edits\n"
    "# will be overwritten on the next session. Add permanent patterns to\n"
    "# <worksite>/simpleharness/config.yaml under permissions.extra_bash_allow.\n"
    "\n"
)


def _append_approved_pattern_unlocked(worksite: Path, pattern: str) -> None:
    """Append `pattern` to <worksite>/simpleharness/config.yaml without
    taking any file lock. Callers MUST already hold an outer lock that
    serializes concurrent writers of config.yaml (e.g. the shared
    ``.approver-refresh.lock`` held by ``persist_approver_allow``, or
    ``_FileLock(cfg_path)`` held by ``append_approved_pattern``).

    Idempotent: no-op if ``pattern`` is already present. Written
    atomically via a temp-file + os.replace.
    """
    sh_dir = worksite / "simpleharness"
    sh_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = sh_dir / "config.yaml"

    if cfg_path.exists():
        with cfg_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{cfg_path}: expected a YAML mapping at top level")
    else:
        data = {}

    perms = data.get("permissions")
    if not isinstance(perms, dict):
        perms = {}
        data["permissions"] = perms

    allow = perms.get("extra_bash_allow")
    if not isinstance(allow, list):
        allow = []
        perms["extra_bash_allow"] = allow

    if pattern in allow:
        return

    allow.append(pattern)

    tmp_path = cfg_path.with_suffix(cfg_path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, default_flow_style=False)
    os.replace(tmp_path, cfg_path)


def append_approved_pattern(worksite: Path, pattern: str) -> None:
    """Append `pattern` to <worksite>/simpleharness/config.yaml under
    permissions.extra_bash_allow. Idempotent: no-op if already present.

    Guarded by a cross-platform file lock and written atomically via a
    temp-file + os.replace. Thin public wrapper around
    ``_append_approved_pattern_unlocked`` — callers that need to hold
    an outer lock (e.g. to refresh the fast-path allowlist atomically)
    should use ``persist_approver_allow`` instead.
    """
    sh_dir = worksite / "simpleharness"
    sh_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = sh_dir / "config.yaml"

    with _FileLock(cfg_path):
        _append_approved_pattern_unlocked(worksite, pattern)


def _write_approver_allowlist_unlocked(task_dir: Path, bash_patterns: list[str]) -> Path:
    """Write .approver-allowlist.txt without taking any file lock.

    Callers MUST already hold an outer lock that serializes concurrent
    writers of the allowlist file (e.g. ``.approver-refresh.lock``
    held by ``persist_approver_allow``, or ``_FileLock(out_path)``
    held by ``write_approver_allowlist``).

    Deduplicates (preserving first-occurrence order), strips whitespace,
    and skips empty entries. Written atomically (tmp + os.replace).
    Returns the absolute path to the written file.
    """
    task_dir.mkdir(parents=True, exist_ok=True)
    out_path = task_dir / ".approver-allowlist.txt"

    seen: set[str] = set()
    ordered: list[str] = []
    for raw in bash_patterns:
        if not isinstance(raw, str):
            continue
        p = raw.strip()
        if not p:
            continue
        if p in seen:
            continue
        seen.add(p)
        ordered.append(p)

    body = _ALLOWLIST_HEADER + "".join(f"{p}\n" for p in ordered)

    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        f.write(body)
    os.replace(tmp_path, out_path)

    return out_path.resolve()


def write_approver_allowlist(task_dir: Path, bash_patterns: list[str]) -> Path:
    """Write .approver-allowlist.txt next to .session_prompt.md.

    The file is one glob pattern per line, with '#'-comment headers.
    The bash fast-path hook reads this file and matches commands against
    the patterns using bash 'case' pattern matching (fnmatch-equivalent).

    Deduplicates (preserving first-occurrence order), strips whitespace,
    and skips empty entries. Written atomically (tmp + os.replace) under
    a ``_FileLock`` on the target file so the fast path never reads a
    torn file and concurrent writers don't collide. Thin public wrapper
    around ``_write_approver_allowlist_unlocked`` — callers that need to
    hold an outer lock across an append-to-config + rewrite-allowlist
    sequence should use ``persist_approver_allow`` instead.

    Returns the absolute path to the written file.
    """
    out_path = task_dir / ".approver-allowlist.txt"
    task_dir.mkdir(parents=True, exist_ok=True)
    with _FileLock(out_path):
        return _write_approver_allowlist_unlocked(task_dir, bash_patterns)


def persist_approver_allow(
    worksite: Path,
    pattern: str,
    task_dir: Path,
) -> list[str]:
    """Append pattern to worksite config AND refresh .approver-allowlist.txt.

    Holds a single file lock for the entire read-append-read-write-write
    sequence so concurrent approver processes cannot race past each other
    and lose patterns. Returns the merged pattern list that was written
    to .approver-allowlist.txt (DEFAULT_BASH_ALLOW + updated
    extra_bash_allow, deduplicated in order).
    """
    lock_path = worksite / "simpleharness" / ".approver-refresh.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _FileLock(lock_path):
        _append_approved_pattern_unlocked(worksite, pattern)
        cfg = load_config(worksite)
        merged = list(DEFAULT_BASH_ALLOW) + list(cfg.permissions.extra_bash_allow)
        _write_approver_allowlist_unlocked(task_dir, merged)
    return merged


# ────────────────────────────────────────────────────────────────────────────
# Version + constants
# ────────────────────────────────────────────────────────────────────────────

VERSION = "0.1.0"

# Terminal styling
console = Console()


def say(msg: str, *, style: str = "cyan") -> None:
    """Print a harness-prefixed message to the terminal."""
    console.print(rf"[{style}]\[harness][/] {msg}")


def warn(msg: str) -> None:
    console.print(rf"[yellow]\[harness WARNING][/] {msg}")


def err(msg: str) -> None:
    console.print(rf"[red]\[harness ERROR][/] {msg}")


# ────────────────────────────────────────────────────────────────────────────
# Workflow loading (config/role loaders live above in this same file;
# pure frontmatter parsing lives in simpleharness.core).
# ────────────────────────────────────────────────────────────────────────────


def load_workflow(name: str) -> Workflow:
    path = toolbox_root() / "workflows" / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"workflow '{name}' not found at {path}")
    meta, body = read_frontmatter_file(path)
    phases_raw = meta.get("phases") or []
    if not isinstance(phases_raw, list) or not all(isinstance(p, str) for p in phases_raw):
        raise ValueError(f"workflow '{name}': phases must be a list of role-name strings")
    return Workflow(
        name=str(meta.get("name", name)),
        phases=tuple(phases_raw),
        max_sessions=meta.get("max_sessions"),
        idle_sleep_seconds=meta.get("idle_sleep_seconds"),
        description=body.strip(),
        source_path=path,
    )


# ────────────────────────────────────────────────────────────────────────────
# STATE.md read/write
# ────────────────────────────────────────────────────────────────────────────

# Field order we preserve when writing STATE.md — mirrors §5 of the plan.
_STATE_FIELD_ORDER = [
    "task_slug",
    "workflow",
    "worksite",
    "toolbox",
    "status",
    "phase",
    "next_role",
    "last_role",
    "total_sessions",
    "session_cap",
    "created",
    "updated",
    "last_session_id",
    "no_progress_ticks",
    "blocked_reason",
    "consecutive_same_role",
]


def read_state(path: Path) -> State:
    meta, _body = read_frontmatter_file(path)
    return State(
        task_slug=str(meta.get("task_slug", "")),
        workflow=str(meta.get("workflow", "")),
        worksite=str(meta.get("worksite", "")),
        toolbox=str(meta.get("toolbox", "")),
        status=str(meta.get("status", "active")),
        phase=str(meta.get("phase", "kickoff")),
        next_role=meta.get("next_role") or None,
        last_role=meta.get("last_role") or None,
        total_sessions=int(meta.get("total_sessions", 0) or 0),
        session_cap=int(meta.get("session_cap", 20) or 20),
        created=str(meta.get("created", "")),
        updated=str(meta.get("updated", "")),
        last_session_id=meta.get("last_session_id") or None,
        no_progress_ticks=int(meta.get("no_progress_ticks", 0) or 0),
        blocked_reason=meta.get("blocked_reason") or None,
        consecutive_same_role=int(meta.get("consecutive_same_role", 0) or 0),
    )


def write_state(path: Path, state: State) -> None:
    data: dict[str, Any] = {
        "task_slug": state.task_slug,
        "workflow": state.workflow,
        "worksite": state.worksite,
        "toolbox": state.toolbox,
        "status": state.status,
        "phase": state.phase,
        "next_role": state.next_role,
        "last_role": state.last_role,
        "total_sessions": state.total_sessions,
        "session_cap": state.session_cap,
        "created": state.created,
        "updated": state.updated,
        "last_session_id": state.last_session_id,
        "no_progress_ticks": state.no_progress_ticks,
        "blocked_reason": state.blocked_reason,
        "consecutive_same_role": state.consecutive_same_role,
    }
    ordered = {k: data[k] for k in _STATE_FIELD_ORDER if k in data}
    yaml_body = yaml.safe_dump(
        ordered, sort_keys=False, default_flow_style=False, allow_unicode=True
    )
    path.write_text(f"---\n{yaml_body}---\n", encoding="utf-8")


def state_hash(path: Path) -> str:
    """Hash of STATE.md content — used for no-progress detection."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ────────────────────────────────────────────────────────────────────────────
# Task discovery
# ────────────────────────────────────────────────────────────────────────────


def worksite_root_from_cwd() -> Path:
    """The worksite is wherever the user invoked `simpleharness` from.

    Override via the global --worksite flag or the SIMPLEHARNESS_WORKSITE
    environment variable.
    """
    env = os.environ.get("SIMPLEHARNESS_WORKSITE")
    if env:
        return Path(env).resolve()
    return Path.cwd().resolve()


def worksite_root(args: argparse.Namespace) -> Path:
    """Resolve the worksite path: --worksite flag > env var > cwd."""
    flag = getattr(args, "worksite", None)
    if flag:
        return Path(flag).resolve()
    return worksite_root_from_cwd()


def discover_tasks(worksite: Path) -> list[Task]:
    """Scan <worksite>/simpleharness/tasks/*/ for task folders with STATE.md."""
    tasks_dir = worksite_sh_dir(worksite) / "tasks"
    if not tasks_dir.exists():
        return []
    out: list[Task] = []
    for child in sorted(tasks_dir.iterdir()):
        if not child.is_dir() or child.name.startswith("_") or child.name.startswith("."):
            continue
        state_path = child / "STATE.md"
        task_md = child / "TASK.md"
        if not state_path.exists():
            continue
        try:
            state = read_state(state_path)
        except (ValueError, yaml.YAMLError) as e:
            warn(f"skipping {child.name}: unreadable STATE.md ({e})")
            continue
        out.append(
            Task(slug=child.name, folder=child, task_md=task_md, state_path=state_path, state=state)
        )
    return out


def write_session_prompt_file(task: Task, prompt: str) -> Path:
    path = task.folder / ".session_prompt.md"
    path.write_text(prompt, encoding="utf-8")
    return path


def consume_correction(task: Task) -> str | None:
    """Read and delete CORRECTION.md if present. Logs to corrections.log."""
    cpath = task.folder / "CORRECTION.md"
    if not cpath.exists():
        return None
    text = cpath.read_text(encoding="utf-8")
    # Audit log
    log_dir = worksite_sh_dir(Path(task.state.worksite)) / "logs" / task.slug
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / "corrections.log").open("a", encoding="utf-8") as f:
        f.write(f"\n----- {now_iso()} -----\n{text}\n")
    cpath.unlink()
    return text


def list_phase_files(task_folder: Path) -> list[Path]:
    """Return existing NN-*.md phase files in numeric order."""
    pat = re.compile(r"^\d\d-.*\.md$")
    out = [p for p in task_folder.iterdir() if p.is_file() and pat.match(p.name)]
    return sorted(out, key=lambda p: p.name)


def _write_approver_settings(task_dir: Path) -> Path:
    """Write .approver-settings.json registering the PreToolUse hook.

    The hook is scoped to the Bash matcher only — other tools flow
    through the normal --allowedTools check. Lifecycle mirrors
    .session_prompt.md: overwritten each session, left on disk for
    post-hoc debugging. Returns the path to the written file.
    """
    hook_script = (toolbox_root() / "simpleharness_approver_hook.sh").as_posix()
    settings = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"bash {hook_script}",
                        }
                    ],
                }
            ]
        }
    }
    out_path = task_dir / ".approver-settings.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
    return out_path


# ────────────────────────────────────────────────────────────────────────────
# claude subprocess invocation
# ────────────────────────────────────────────────────────────────────────────


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
        bufsize=1,  # line-buffered
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


# ────────────────────────────────────────────────────────────────────────────
# Stream-JSON reader + pretty printer + logger
# ────────────────────────────────────────────────────────────────────────────


def _pretty_event(event: dict[str, Any]) -> None:
    """Render a single stream-json event to the terminal using rich."""
    etype = event.get("type", "")
    # Partial-message deltas are pure noise at the terminal level. They still
    # land in the .jsonl log for debug; just don't spam the user here.
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

                console.print(rf"[magenta]→ {tname}[/] [dim]{_rich_escape(pretty)}[/]")
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
                    console.print(rf"[red]  ✗ error[/] [dim]{_rich_escape(preview)}[/]{suffix}")
                else:
                    preview = text[:800]
                    suffix = f" [dim](+{len(text) - 800} more)[/]" if len(text) > 800 else ""
                    console.print(rf"[green]  ← result[/] [dim]{_rich_escape(preview)}[/]{suffix}")
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
    # unknown type - show briefly
    console.print(rf"[dim]  \[{etype}][/]")


def stream_and_log(
    proc: subprocess.Popen[str],
    jsonl_log: Path,
    plain_log: Path,
) -> tuple[str | None, str | None]:
    """Read proc.stdout line-by-line, pretty-print + log, return (session_id, result_text).

    Raises KeyboardInterrupt up to the caller unchanged if SIGINT happens.
    """
    session_id: str | None = None
    result_text: str | None = None
    jsonl_log.parent.mkdir(parents=True, exist_ok=True)
    with (
        jsonl_log.open("w", encoding="utf-8") as jf,
        plain_log.open("w", encoding="utf-8") as pf,
    ):
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            if not line.strip():
                continue
            # raw JSONL log always
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
            # capture identifiers
            if isinstance(event, dict):
                if event.get("type") == "system" and event.get("subtype") == "init":
                    session_id = event.get("session_id") or session_id
                elif event.get("type") == "result":
                    session_id = event.get("session_id") or session_id
                    result_text = event.get("result")
            # plain-text mirror: assistant text + tool calls + tool results,
            # so the .log reads like a human-readable session shadow.
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
                            pf.write(f"→ {tname} {_format_tool_call(tname, tinput)}\n")
                elif etype == "user":
                    for block in event.get("message", {}).get("content", []) or []:
                        if block.get("type") == "tool_result":
                            content = block.get("content", "")
                            if isinstance(content, list):
                                content = "\n".join(
                                    c.get("text", "") for c in content if isinstance(c, dict)
                                )
                            marker = "✗" if block.get("is_error") else "←"
                            pf.write(f"  {marker} {str(content)[:800]}\n")
            pf.flush()
            _pretty_event(event if isinstance(event, dict) else {"type": "unknown"})
    return session_id, result_text


# ────────────────────────────────────────────────────────────────────────────
# SIGINT handler + stdin correction capture
# ────────────────────────────────────────────────────────────────────────────


class InterventionState:
    """Mutable flag a signal handler can flip without raising mid-read."""

    abort: bool = False


_intervention = InterventionState()


def read_stdin_until_blank() -> list[str]:
    """Read lines from stdin until a blank line or second Ctrl+C.

    A single Ctrl+C during this read sets _intervention.abort=True and returns
    whatever has been typed so far. The caller decides whether to also quit.
    """
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
    path = task.folder / "CORRECTION.md"
    body = "\n".join(lines).strip()
    path.write_text(body + "\n", encoding="utf-8")
    return path


# ────────────────────────────────────────────────────────────────────────────
# Session runner
# ────────────────────────────────────────────────────────────────────────────


def run_session(task: Task, role: Role, workflow: Workflow, config: Config) -> SessionResult:
    """Build prompt, spawn claude, stream output, handle SIGINT. Single session."""
    toolbox = toolbox_root()

    # 1. consume correction file if present
    correction = consume_correction(task)

    # 2. build and write prompt
    phase_files = list_phase_files(task.folder)
    prompt = build_session_prompt(task, role, workflow, toolbox, correction, phase_files)
    prompt_file = write_session_prompt_file(task, prompt)

    # 3. log paths (built before the command so approver mode can point the
    # MCP server at the jsonl log it needs to tail)
    log_root = worksite_sh_dir(Path(task.state.worksite)) / "logs" / task.slug
    idx = task.state.total_sessions  # zero-padded to 2
    stem = f"{idx:02d}-{role.name}"
    jsonl_log = log_root / f"{stem}.jsonl"
    plain_log = log_root / f"{stem}.log"

    # 4. build command — pre-write approver files in shell before calling core
    session_id = str(uuid.uuid4())
    if config.permissions.mode == "approver":
        bash_patterns = list(DEFAULT_BASH_ALLOW) + list(config.permissions.extra_bash_allow)
        write_approver_allowlist(task.folder, bash_patterns)
        approver_settings_path = _write_approver_settings(task.folder)
    else:
        approver_settings_path = None
    cmd = build_claude_cmd(
        prompt_file,
        role,
        toolbox,
        session_id,
        config,
        approver_settings_path=approver_settings_path,
    )

    # 5. banner
    console.rule(f"[cyan]session {idx + 1}  [bold]{role.name}[/]  task={task.slug}")
    say(
        f"model={config.model}  session_id={session_id[:8]}  max_turns={role.max_turns or config.max_turns_default}"
    )
    if correction:
        say("CORRECTION.md was consumed and injected into this session's prompt.", style="yellow")

    # 6. env exports (approver mode only — mirror the vars Claude Code will
    # pass to the MCP server, so anything running in-process downstream sees
    # the same values)
    extra_env: dict[str, str] | None = None
    if config.permissions.mode == "approver":
        extra_env = {
            "SIMPLEHARNESS_STREAM_LOG": str(jsonl_log),
            "SIMPLEHARNESS_WORKSITE": str(Path(task.state.worksite)),
            "SIMPLEHARNESS_APPROVER_MODEL": config.permissions.approver_model,
            "SIMPLEHARNESS_ROLE": role.name,
            "SIMPLEHARNESS_TASK_SLUG": task.slug,
        }

    # 7. spawn + stream
    proc = spawn_claude(cmd, Path(task.state.worksite), extra_env=extra_env)
    interrupted = False
    result_session_id: str | None = None
    result_text: str | None = None
    try:
        result_session_id, result_text = stream_and_log(proc, jsonl_log, plain_log)
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
            say("second Ctrl+C detected — aborting harness")
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
    )


# ────────────────────────────────────────────────────────────────────────────
# Single-tick loop (MVP = watch --once)
# ────────────────────────────────────────────────────────────────────────────


def _try_load_workflow(name: str) -> Workflow | None:
    """Load a workflow by name; return None on any error."""
    try:
        return load_workflow(name)
    except (FileNotFoundError, ValueError):
        return None


def _task_by_slug(tasks: tuple[Task, ...], slug: str | None) -> Task:
    """Return the Task matching slug. Raises ValueError if not found."""
    for t in tasks:
        if t.slug == slug:
            return t
    raise ValueError(f"task slug {slug!r} not found in task list")


def tick_once(worksite: Path, config: Config) -> bool:
    """One iteration of the loop. Returns True if we ran a session, False if idle."""
    tasks = tuple(discover_tasks(worksite))
    corrections = frozenset(t.slug for t in tasks if (t.folder / "CORRECTION.md").exists())
    workflows_by_name: dict[str, Workflow | None] = {
        t.state.workflow: _try_load_workflow(t.state.workflow) for t in tasks
    }
    plan = plan_tick(tasks, workflows_by_name, corrections, config)

    match plan.kind:
        case "no_tasks":
            say("no tasks in simpleharness/tasks/", style="dim")
            return False
        case "no_active":
            say("no active tasks", style="dim")
            return False
        case "block":
            task = _task_by_slug(tasks, plan.block_task_slug)
            err(f"task {task.slug}: {plan.block_reason}")
            new_state = replace(task.state, status="blocked", blocked_reason=plan.block_reason)
            write_state(task.state_path, new_state)
            return False
        case "run":
            task = _task_by_slug(tasks, plan.run_task_slug)
            role_name = plan.run_role_name
            assert role_name is not None  # guaranteed by plan_tick

            # log correction/loopback messages
            correction_pending = task.slug in corrections
            if correction_pending:
                say(
                    f"task {task.slug}: CORRECTION.md present — re-running {role_name}",
                    style="yellow",
                )
            else:
                workflow = workflows_by_name[task.state.workflow]
                assert workflow is not None
                resolved = resolve_next_role(task, workflow)
                if resolved is None:
                    say(
                        f"task {task.slug}: past final phase, looping back to {role_name}",
                        style="yellow",
                    )

            try:
                role = load_role(role_name)
            except (FileNotFoundError, ValueError) as e:
                err(f"task {task.slug}: {e}")
                new_state = replace(
                    task.state, status="blocked", blocked_reason=f"role load failed: {e}"
                )
                write_state(task.state_path, new_state)
                return False

            # No-progress detection: pre_hash is taken BEFORE run_session, post_hash
            # AFTER. The old apply_session_bookkeeping wrote harness fields
            # (total_sessions, updated, last_role) BEFORE post_hash was taken, so
            # post_hash always differed from pre_hash regardless of agent work —
            # effectively masking the no-progress signal entirely. The new flow
            # captures post_hash before compute_post_session_state writes anything,
            # so post_hash == pre_hash now correctly means "agent made no edits to
            # STATE.md during this session." Agents that work in source files
            # without updating STATE.md will accumulate no_progress_ticks faster
            # than under the old code. The default threshold is a warning, not a
            # block, so this only surfaces as a soft nag.
            pre_hash = state_hash(task.state_path)

            # clear any stale next_role override (consumed by this session)
            if task.state.next_role:
                cleared_state = replace(task.state, next_role=None)
                write_state(task.state_path, cleared_state)

            workflow = workflows_by_name[task.state.workflow]
            assert workflow is not None

            # save pre-session counters for compute_post_session_state
            prev_last_role = task.state.last_role
            prev_consecutive_same_role = task.state.consecutive_same_role

            try:
                session = run_session(task, role, workflow, config)
            except KeyboardInterrupt:
                say("aborted by user, exiting")
                raise

            post_hash = state_hash(task.state_path)
            current_state = read_state(task.state_path)

            new_state = compute_post_session_state(
                current_state,
                role.name,
                session,
                prev_last_role=prev_last_role,
                prev_consecutive_same_role=prev_consecutive_same_role,
                pre_hash=pre_hash,
                post_hash=post_hash,
                config=config,
                now=datetime.now(UTC),
            )

            # Warn only on the tick that first crosses the threshold, not every tick after.
            if (
                new_state.no_progress_ticks >= config.no_progress_tick_threshold
                and new_state.no_progress_ticks > current_state.no_progress_ticks
            ):
                warn(f"task {task.slug}: no progress for {new_state.no_progress_ticks} ticks")

            write_state(task.state_path, new_state)
            say(
                f"task {task.slug}: session complete  "
                f"(status={new_state.status}, next_role={new_state.next_role or 'auto'})"
            )
            return True
        case _:
            return False


# ────────────────────────────────────────────────────────────────────────────
# CLI commands
# ────────────────────────────────────────────────────────────────────────────


def _next_task_index(tasks_dir: Path) -> int:
    if not tasks_dir.exists():
        return 1
    highest = 0
    for child in tasks_dir.iterdir():
        if not child.is_dir():
            continue
        m = re.match(r"^(\d{3})-", child.name)
        if m:
            highest = max(highest, int(m.group(1)))
    return highest + 1


def cmd_init(args: argparse.Namespace) -> int:
    worksite = worksite_root(args)
    sh = worksite_sh_dir(worksite)
    for sub in ("tasks", "memory", "logs"):
        (sh / sub).mkdir(parents=True, exist_ok=True)
    memory_file = sh / "memory" / "WORKSITE.md"
    if not memory_file.exists():
        memory_file.write_text(
            "# Worksite memory\n\nLong-term notes that every session can read.\n",
            encoding="utf-8",
        )
    say(f"initialized {sh}")
    return 0


def cmd_new(args: argparse.Namespace) -> int:
    worksite = worksite_root(args)
    sh = worksite_sh_dir(worksite)
    if not sh.exists():
        warn("simpleharness/ folder not found — running `init` first")
        cmd_init(args)
    tasks_dir = sh / "tasks"
    idx = _next_task_index(tasks_dir)
    slug = f"{idx:03d}-{_slugify(args.title)}"
    folder = tasks_dir / slug
    folder.mkdir(parents=True)

    # TASK.md
    task_frontmatter = {
        "title": args.title,
        "workflow": args.workflow,
        "worksite": ".",
    }
    task_body = (
        "# Goal\n\n"
        "<describe what you want done. be specific about constraints.>\n\n"
        "## Constraints\n\n"
        "- <any boundaries>\n"
    )
    yaml_fm = yaml.safe_dump(task_frontmatter, sort_keys=False, allow_unicode=True)
    (folder / "TASK.md").write_text(f"---\n{yaml_fm}---\n\n{task_body}", encoding="utf-8")

    # STATE.md
    state = State(
        task_slug=slug,
        workflow=args.workflow,
        worksite=str(worksite),
        toolbox=str(toolbox_root()),
        status="active",
        phase="kickoff",
        next_role=None,
        last_role=None,
        total_sessions=0,
        session_cap=20,
        created=now_iso(),
        updated=now_iso(),
    )
    write_state(folder / "STATE.md", state)
    say(f"created task {slug} at {folder}")
    say(f"edit {folder / 'TASK.md'} to describe your goal, then run: simpleharness watch --once")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    worksite = worksite_root(args)
    config = load_config(worksite)
    if not worksite_sh_dir(worksite).exists():
        warn("simpleharness/ folder not found — running `init` first")
        cmd_init(args)
    # SIGINT must be caught in the harness, not propagated to child automatically.
    # On Windows this is handled per-spawn via CREATE_NEW_PROCESS_GROUP;
    # on Unix, Python default already raises KeyboardInterrupt to the main thread.
    try:
        if args.once:
            tick_once(worksite, config)
            return 0
        say(
            f"starting watch loop (idle sleep = {config.idle_sleep_seconds}s). Ctrl+C to interrupt."
        )
        while True:
            did_work = tick_once(worksite, config)
            if not did_work:
                time.sleep(config.idle_sleep_seconds)
    except KeyboardInterrupt:
        say("stopped by user")
        return 0


def cmd_status(args: argparse.Namespace) -> int:
    worksite = worksite_root(args)
    tasks = discover_tasks(worksite)
    if not tasks:
        say("no tasks")
        return 0
    for t in tasks:
        line = (
            f"{t.slug}  status={t.state.status}  phase={t.state.phase}  "
            f"last={t.state.last_role or '-'}  next={t.state.next_role or '-'}  "
            f"sessions={t.state.total_sessions}/{t.state.session_cap}"
        )
        console.print(line)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    return cmd_status(args)


def cmd_show(args: argparse.Namespace) -> int:
    worksite = worksite_root(args)
    tasks = {t.slug: t for t in discover_tasks(worksite)}
    t = tasks.get(args.slug)
    if not t:
        err(f"task '{args.slug}' not found")
        return 1
    console.rule(t.slug)
    console.print(f"status: {t.state.status}")
    console.print(f"phase: {t.state.phase}")
    console.print(f"workflow: {t.state.workflow}")
    console.print(f"worksite: {t.state.worksite}")
    console.print(f"last_role: {t.state.last_role}")
    console.print(f"next_role: {t.state.next_role}")
    console.print(f"sessions: {t.state.total_sessions}/{t.state.session_cap}")
    if t.state.blocked_reason:
        console.print(f"blocked_reason: {t.state.blocked_reason}")
    console.rule("files")
    for p in sorted(t.folder.iterdir()):
        if p.is_file():
            console.print(f"  {p.name}")
    return 0


def cmd_unblock(args: argparse.Namespace) -> int:
    """Reset a blocked task back to active so `watch` picks it up again.

    Matches on exact slug or unique substring so users don't have to type the
    full `NNN-long-slug` form.
    """
    worksite = worksite_root(args)
    tasks = discover_tasks(worksite)
    matches = [t for t in tasks if t.slug == args.slug or args.slug in t.slug]
    if not matches:
        err(f"no task matches '{args.slug}'")
        return 1
    if len(matches) > 1:
        err(f"'{args.slug}' matches multiple tasks: {', '.join(t.slug for t in matches)}")
        return 1
    target = matches[0]
    state = read_state(target.state_path)
    if state.status != "blocked":
        warn(f"task {target.slug} is {state.status}, not blocked — nothing to do")
        return 0
    prev = state.blocked_reason or "(none)"
    new_state = replace(state, status="active", blocked_reason=None, no_progress_ticks=0)
    write_state(target.state_path, new_state)
    say(f"unblocked {target.slug} (was: {prev})")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    worksite = worksite_root(args)
    config = load_config(worksite)
    ok = True

    # claude on PATH?
    try:
        proc = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0:
            say(f"claude CLI found: {proc.stdout.strip()}", style="green")
        else:
            err(f"claude --version exited {proc.returncode}: {proc.stderr.strip()}")
            ok = False
    except FileNotFoundError:
        err("claude CLI not found on PATH")
        ok = False
    except subprocess.TimeoutExpired:
        err("claude --version timed out")
        ok = False

    # toolbox reachable?
    tb = toolbox_root()
    if (tb / "harness.py").exists():
        say(f"toolbox: {tb}", style="green")
    else:
        err(f"toolbox path wrong: {tb}")
        ok = False

    # roles + workflows present?
    roles_dir = tb / "roles"
    workflows_dir = tb / "workflows"
    roles = sorted(p.stem for p in roles_dir.glob("*.md")) if roles_dir.exists() else []
    flows = sorted(p.stem for p in workflows_dir.glob("*.md")) if workflows_dir.exists() else []
    say(f"roles: {', '.join(roles) or '(none)'}")
    say(f"workflows: {', '.join(flows) or '(none)'}")
    if not roles:
        err("no roles found")
        ok = False
    if not flows:
        err("no workflows found")
        ok = False

    # permission mode
    mode = config.permissions.mode
    if mode == "dangerous":
        warn("permissions.mode=dangerous — checking for sandbox marker")
        in_sandbox = Path("/.dockerenv").exists() or os.environ.get("SIMPLEHARNESS_SANDBOX") == "1"
        if in_sandbox:
            say("sandbox marker detected — dangerous mode allowed", style="green")
        else:
            err(
                "permissions.mode=dangerous but no sandbox marker. "
                "Watch will refuse to run unless --i-know-its-dangerous is passed."
            )
            ok = False
    elif mode == "approver":
        say(
            "permission mode: APPROVER (acceptEdits + PreToolUse hook review)",
            style="green",
        )
        approver_ok = True

        # claude supports --settings?
        try:
            help_proc = subprocess.run(
                ["claude", "--help"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            help_text = (help_proc.stdout or "") + (help_proc.stderr or "")
            if "--settings" not in help_text:
                err(
                    "Claude Code CLI does not support --settings — "
                    "upgrade the CLI to enable approver mode."
                )
                approver_ok = False
            else:
                say("claude CLI supports --settings", style="green")
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            err(f"could not run `claude --help` to check approver support: {e}")
            approver_ok = False

        # bash on PATH (the fast-path PreToolUse hook is a .sh script)
        try:
            bash_proc = subprocess.run(
                ["bash", "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                stdin=subprocess.DEVNULL,
            )
            if bash_proc.returncode == 0:
                first_line = (bash_proc.stdout or "").splitlines()[0:1]
                say(
                    f"bash found: {first_line[0] if first_line else 'ok'}",
                    style="green",
                )
            else:
                err(f"bash --version exited {bash_proc.returncode}")
                approver_ok = False
        except FileNotFoundError:
            err("bash not found on PATH — required to run the approver fast-path hook")
            approver_ok = False
        except subprocess.TimeoutExpired:
            err("bash --version timed out")
            approver_ok = False

        # uv on PATH (belt-and-braces for the slow-path Python hook)
        try:
            uv_proc = subprocess.run(
                ["uv", "--version"], capture_output=True, text=True, timeout=10
            )
            if uv_proc.returncode == 0:
                say(f"uv found: {uv_proc.stdout.strip()}", style="green")
            else:
                err(f"uv --version exited {uv_proc.returncode}: {uv_proc.stderr.strip()}")
                approver_ok = False
        except FileNotFoundError:
            err("uv not found on PATH")
            approver_ok = False
        except subprocess.TimeoutExpired:
            err("uv --version timed out")
            approver_ok = False

        # bash fast-path script present in the toolbox?
        hook_sh = toolbox_root() / "simpleharness_approver_hook.sh"
        if hook_sh.is_file():
            say(f"approver hook script: {hook_sh}", style="green")
        else:
            err(f"approver hook script missing: {hook_sh}")
            approver_ok = False

        # Python slow-path module importable?
        import importlib.util

        if importlib.util.find_spec("simpleharness.approver_shell") is None:
            err("simpleharness.approver_shell module not importable")
            approver_ok = False
        else:
            say("simpleharness.approver_shell: importable", style="green")

        # approver role file loadable?
        try:
            load_role("approver")
            say("roles/approver.md: loadable", style="green")
        except (FileNotFoundError, ValueError) as e:
            err(f"roles/approver.md: {e}")
            approver_ok = False

        if approver_ok:
            say("✓ approver mode ready", style="green")
        else:
            ok = False
    else:
        say("permission mode: SAFE (acceptEdits + curated allowlist)", style="green")

    # current worksite
    sh = worksite_sh_dir(worksite)
    if sh.exists():
        say(f"worksite simpleharness/ dir: {sh}", style="green")
    else:
        warn("worksite simpleharness/ dir missing — run `simpleharness init`")

    return 0 if ok else 1


# ────────────────────────────────────────────────────────────────────────────
# main / argparse
# ────────────────────────────────────────────────────────────────────────────


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="simpleharness",
        description="Lightweight baton-pass agent harness over the Claude Code CLI",
    )
    p.add_argument("--version", action="version", version=f"simpleharness {VERSION}")

    # Common flags every subcommand inherits.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--worksite",
        metavar="PATH",
        help="worksite path (default: current directory or $SIMPLEHARNESS_WORKSITE)",
    )

    sub = p.add_subparsers(dest="command")

    p_init = sub.add_parser("init", parents=[common], help="create simpleharness/ folder layout")
    p_init.set_defaults(func=cmd_init)

    p_new = sub.add_parser("new", parents=[common], help="scaffold a new task")
    p_new.add_argument("title", help="one-line task title")
    p_new.add_argument("--workflow", default="universal", help="workflow name (default: universal)")
    p_new.set_defaults(func=cmd_new)

    p_watch = sub.add_parser("watch", parents=[common], help="long-lived loop (primary mode)")
    p_watch.add_argument("--once", action="store_true", help="do one tick then exit")
    p_watch.add_argument(
        "--i-know-its-dangerous",
        action="store_true",
        help="override sandbox check when permissions.mode=dangerous",
    )
    p_watch.set_defaults(func=cmd_watch)

    p_status = sub.add_parser("status", parents=[common], help="list active tasks + current phase")
    p_status.set_defaults(func=cmd_status)

    p_list = sub.add_parser("list", parents=[common], help="list all tasks")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", parents=[common], help="show details of one task")
    p_show.add_argument("slug")
    p_show.set_defaults(func=cmd_show)

    p_unblock = sub.add_parser(
        "unblock",
        parents=[common],
        help="reset a blocked task to active (clears blocked_reason)",
    )
    p_unblock.add_argument("slug", help="task slug or unique substring")
    p_unblock.set_defaults(func=cmd_unblock)

    p_doctor = sub.add_parser("doctor", parents=[common], help="sanity checks")
    p_doctor.set_defaults(func=cmd_doctor)

    return p


def main() -> int:
    parser = build_argparser()
    args = parser.parse_args()
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        say("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
