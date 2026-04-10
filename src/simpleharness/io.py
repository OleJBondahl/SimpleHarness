"""File I/O, config loading, state management, and locking functions.

Extracted from shell.py — pure extraction, no behavior changes.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from simpleharness.core import (
    _VALID_APPROVER_MODELS,
    _VALID_MODES,
    _VALID_SKILL_ENFORCEMENT,
    DEFAULT_BASH_ALLOW,
    Config,
    Permissions,
    Role,
    SkillsConfig,
    State,
    Subagent,
    Task,
    Workflow,
    _merge_config,
    build_session_hooks_config,
    parse_frontmatter,
    parse_skill_list,
    toolbox_root,
)
from simpleharness.process import pid_alive

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

    skills_raw = merged.get("skills", {}) or {}
    skills_list = parse_skill_list(
        {
            "available": skills_raw.get("default_available"),
            "must_use": skills_raw.get("default_must_use"),
        }
        if skills_raw
        else None
    )
    enforcement = skills_raw.get("enforcement", "strict")
    if enforcement is None:
        enforcement = "strict"
    if not isinstance(enforcement, str) or enforcement not in _VALID_SKILL_ENFORCEMENT:
        raise ValueError(
            f"skills.enforcement: invalid value {enforcement!r}; "
            f"must be one of {_VALID_SKILL_ENFORCEMENT}"
        )
    skills_cfg = SkillsConfig(
        default_available=skills_list.available,
        default_must_use=skills_list.must_use,
        enforcement=enforcement,
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
        skills=skills_cfg,
    )


def load_role(name: str) -> Role:
    path = toolbox_root() / "roles" / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"role '{name}' not found at {path}")
    meta, body = read_frontmatter_file(path)
    skills = parse_skill_list(meta.get("skills"))
    return Role(
        name=str(meta.get("name", name)),
        body=body.strip(),
        description=str(meta.get("description", "")),
        model=meta.get("model"),
        max_turns=meta.get("max_turns"),
        allowed_tools=tuple(meta.get("allowed_tools", []) or []),
        privileged=bool(meta.get("privileged", False)),
        source_path=path,
        skills=skills,
    )


def load_subagent(name: str) -> Subagent:
    path = toolbox_root() / "subagents" / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"subagent '{name}' not found at {path}")
    meta, body = read_frontmatter_file(path)
    if meta.get("invocation") == "mcp-permission-handler":
        raise ValueError(
            f"subagent '{name}': 'invocation: mcp-permission-handler' is only "
            "valid for main roles under roles/, not subagents/"
        )
    skills = parse_skill_list(meta.get("skills"))
    return Subagent(
        name=str(meta.get("name", name)),
        body=body.strip(),
        description=str(meta.get("description", "")),
        model=meta.get("model"),
        # Intentional backward-compatibility: accept role-style `allowed_tools:`
        # frontmatter as a fallback when `tools:` is absent, so subagent files
        # written in the same style as roles still load correctly.
        tools=tuple(meta.get("tools", meta.get("allowed_tools", [])) or []),
        source_path=path,
        skills=skills,
    )


def load_all_subagents() -> tuple[Subagent, ...]:
    subagents_dir = toolbox_root() / "subagents"
    if not subagents_dir.exists():
        return ()
    out = []
    for path in sorted(subagents_dir.glob("*.md")):
        out.append(load_subagent(path.stem))
    return tuple(out)


# ────────────────────────────────────────────────────────────────────────────
# File locking (impure — os, time)
# ────────────────────────────────────────────────────────────────────────────


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
                        if other_pid > 0 and not pid_alive(other_pid):
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
# Workflow loading
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
    "total_cost_usd",
    "no_progress_ticks",
    "blocked_reason",
    "consecutive_same_role",
    "retry_count",
    "retry_after",
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
        total_cost_usd=float(meta.get("total_cost_usd", 0.0) or 0.0),
        no_progress_ticks=int(meta.get("no_progress_ticks", 0) or 0),
        blocked_reason=meta.get("blocked_reason") or None,
        consecutive_same_role=int(meta.get("consecutive_same_role", 0) or 0),
        retry_count=int(meta.get("retry_count", 0) or 0),
        retry_after=meta.get("retry_after") or None,
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
        "total_cost_usd": state.total_cost_usd,
        "no_progress_ticks": state.no_progress_ticks,
        "blocked_reason": state.blocked_reason,
        "consecutive_same_role": state.consecutive_same_role,
        "retry_count": state.retry_count,
        "retry_after": state.retry_after,
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
# Session file helpers
# ────────────────────────────────────────────────────────────────────────────


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
    # task.folder = <worksite>/simpleharness/tasks/<slug>/, so .parent.parent = simpleharness/
    log_dir = task.folder.parent.parent / "logs" / task.slug
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


def _write_approver_settings(task_dir: Path, enforcement_mode: str = "off") -> Path:
    """Write .approver-settings.json registering the PreToolUse hook.

    The hook is scoped to the Bash matcher only — other tools flow
    through the normal --allowedTools check. Also merges skill enforcement
    hooks (SessionStart/Stop/SubagentStop) when enforcement_mode != 'off'.
    Lifecycle mirrors .session_prompt.md: overwritten each session, left on
    disk for post-hoc debugging. Returns the path to the written file.
    """
    hook_script = (toolbox_root() / "simpleharness_approver_hook.sh").as_posix()
    approver_hooks: dict[str, Any] = {
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
    merged_hooks = build_session_hooks_config(
        enforcement_mode, sys.executable, existing_hooks=approver_hooks
    )
    settings = {"hooks": merged_hooks}
    out_path = task_dir / ".approver-settings.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
    return out_path


def _write_session_settings(task_dir: Path, enforcement_mode: str) -> Path | None:
    """Write .session-settings.json with skill hooks for non-approver modes.

    Returns the path to the written file, or None if enforcement is 'off'
    (nothing to register).
    """
    if enforcement_mode == "off":
        return None
    hooks = build_session_hooks_config(enforcement_mode, sys.executable)
    settings = {"hooks": hooks}
    out_path = task_dir / ".session-settings.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
    return out_path
