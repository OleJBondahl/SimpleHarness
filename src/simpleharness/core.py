"""Shared loader + config helpers for SimpleHarness.

Extracted from harness.py so the approver MCP server (and any future
side-processes) can import them without pulling in the subprocess /
streaming machinery.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# ────────────────────────────────────────────────────────────────────────────
# Dataclasses: Permissions, Config, Role
# ────────────────────────────────────────────────────────────────────────────

_VALID_MODES = ("safe", "approver", "dangerous")
_VALID_APPROVER_MODELS = ("haiku", "sonnet", "opus")


# Default Bash command glob patterns that are allowed in safe mode. Each entry
# is the content inside the ``Bash(...)`` wrapper Claude Code uses for its
# permission rules. Users extend via config.yaml ``permissions.extra_bash_allow``.
# Moved here from harness.py so the approver PreToolUse hook (which must not
# depend on harness.py) can import it.
DEFAULT_BASH_ALLOW: list[str] = [
    "git status",
    "git diff *",
    "git log *",
    "git add *",
    "git commit *",
    "git stash *",
    "git restore *",
    "git checkout *",
    "git branch *",
    "git show *",
    "uv run *",
    "uv sync",
    "uv add *",
    "npm run *",
    "npm test *",
    "pytest *",
    "ruff *",
    "ty *",
    "python -m *",
    "node *",
    "ls *",
    "cat *",
    "* --version",
    "* --help *",
]


@dataclass
class Permissions:
    mode: str = "safe"
    approver_model: str = "sonnet"
    escalate_denials_to_correction: bool = False
    extra_bash_allow: list[str] = field(default_factory=list)
    extra_tools_allow: list[str] = field(default_factory=list)


@dataclass
class Config:
    model: str = "opus"
    idle_sleep_seconds: int = 30
    max_sessions_per_task: int = 20
    max_same_role_repeats: int = 3
    no_progress_tick_threshold: int = 5
    max_turns_default: int = 60
    include_partial_messages: bool = True
    permissions: Permissions = field(default_factory=Permissions)


@dataclass
class Role:
    name: str
    body: str  # the system prompt body (frontmatter stripped)
    description: str = ""
    model: str | None = None
    max_turns: int | None = None
    allowed_tools: list[str] = field(default_factory=list)
    privileged: bool = False
    source_path: Path | None = None


# ────────────────────────────────────────────────────────────────────────────
# YAML frontmatter helpers
# ────────────────────────────────────────────────────────────────────────────

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse a markdown file with YAML frontmatter. Returns (metadata, body).

    If no frontmatter is present, returns ({}, text).
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    meta_raw, body = m.group(1), m.group(2)
    try:
        meta = yaml.safe_load(meta_raw) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"invalid YAML frontmatter: {e}") from e
    if not isinstance(meta, dict):
        raise ValueError("frontmatter must be a mapping")
    return meta, body


def read_frontmatter_file(path: Path) -> tuple[dict[str, Any], str]:
    return parse_frontmatter(path.read_text(encoding="utf-8"))


# ────────────────────────────────────────────────────────────────────────────
# Config loading (toolbox default + worksite override)
# ────────────────────────────────────────────────────────────────────────────


def toolbox_root() -> Path:
    """The toolbox repo root (where this module lives)."""
    return Path(__file__).resolve().parent


def _load_yaml_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a YAML mapping at top level")
    return data


def _merge_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_config(out[k], v)
        else:
            out[k] = v
    return out


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
        extra_bash_allow=list(perms_raw.get("extra_bash_allow", []) or []),
        extra_tools_allow=list(perms_raw.get("extra_tools_allow", []) or []),
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


# ────────────────────────────────────────────────────────────────────────────
# Role loading
# ────────────────────────────────────────────────────────────────────────────


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
        allowed_tools=list(meta.get("allowed_tools", []) or []),
        privileged=bool(meta.get("privileged", False)),
        source_path=path,
    )


# ────────────────────────────────────────────────────────────────────────────
# append_approved_pattern: called by the approver MCP server on allow verdicts
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


# ────────────────────────────────────────────────────────────────────────────
# write_approver_allowlist: the fast-path file the bash PreToolUse hook reads
# ────────────────────────────────────────────────────────────────────────────


_ALLOWLIST_HEADER = (
    "# simpleharness approver fast-path allowlist\n"
    "# Regenerated by the harness on session spawn and by the Python slow\n"
    "# path after an approver allow verdict. Do not hand-edit — your edits\n"
    "# will be overwritten on the next session. Add permanent patterns to\n"
    "# <worksite>/simpleharness/config.yaml under permissions.extra_bash_allow.\n"
    "\n"
)


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
# Dataclasses: Workflow, State, Task, SessionResult
# (moved from shell.py — Phase 2b)
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class Workflow:
    name: str
    phases: list[str]
    max_sessions: int | None = None
    idle_sleep_seconds: int | None = None
    description: str = ""
    source_path: Path | None = None


@dataclass
class State:
    # identity
    task_slug: str
    workflow: str
    worksite: str
    toolbox: str
    # lifecycle
    status: str = "active"  # active | blocked | done | paused
    phase: str = "kickoff"
    next_role: str | None = None
    last_role: str | None = None
    # bookkeeping
    total_sessions: int = 0
    session_cap: int = 20
    created: str = ""
    updated: str = ""
    last_session_id: str | None = None
    # anti-stall
    no_progress_ticks: int = 0
    # human-facing
    blocked_reason: str | None = None
    # consecutive same-role counter (harness-managed, not spec'd in plan but needed)
    consecutive_same_role: int = 0


@dataclass
class Task:
    slug: str
    folder: Path
    task_md: Path
    state_path: Path
    state: State


@dataclass
class SessionResult:
    completed: bool  # true if claude exited naturally
    interrupted: bool  # true if user Ctrl+C'd
    session_id: str | None
    result_text: str | None
    exit_code: int | None


# ────────────────────────────────────────────────────────────────────────────
# Pure helpers (moved from shell.py — Phase 2b)
# ────────────────────────────────────────────────────────────────────────────

# Default tool names that are always allowed in safe mode.
DEFAULT_TOOLS_ALLOW: list[str] = [
    "Edit",
    "Write",
    "MultiEdit",
    "Read",
    "Glob",
    "Grep",
    "NotebookEdit",
    "Agent",
]


def worksite_sh_dir(worksite: Path) -> Path:
    return worksite / "simpleharness"


def pick_next_task(tasks: list[Task]) -> Task | None:
    """Priority: CORRECTION.md exists > active non-blocked > lowest slug."""
    candidates = [t for t in tasks if t.state.status == "active"]
    if not candidates:
        return None
    # tasks with CORRECTION.md take priority
    with_correction = [t for t in candidates if (t.folder / "CORRECTION.md").exists()]
    if with_correction:
        return sorted(with_correction, key=lambda t: t.slug)[0]
    return sorted(candidates, key=lambda t: t.slug)[0]


def resolve_next_role(task: Task, workflow: Workflow) -> str | None:
    """Hybrid: STATE.next_role wins if set, else advance along workflow.phases.

    Returns None if the task is past its final phase (should be marked done).
    """
    if task.state.status != "active":
        return None
    if task.state.next_role:
        return task.state.next_role
    phases = workflow.phases
    if not phases:
        return None
    last = task.state.last_role
    if last is None:
        return phases[0]
    try:
        idx = phases.index(last)
    except ValueError:
        # last_role not in workflow — restart from beginning
        return phases[0]
    if idx + 1 >= len(phases):
        return None  # past the final phase
    return phases[idx + 1]


def list_phase_files(task_folder: Path) -> list[Path]:
    """Return existing NN-*.md phase files in numeric order."""
    pat = re.compile(r"^\d\d-.*\.md$")
    out = [p for p in task_folder.iterdir() if p.is_file() and pat.match(p.name)]
    return sorted(out, key=lambda p: p.name)


def build_session_prompt(
    task: Task,
    role: Role,
    workflow: Workflow,
    toolbox: Path,
    correction_text: str | None,
) -> str:
    """Assemble the spatial-awareness preamble + phase instructions.

    Returns the full text. Caller writes it to <task>/.session_prompt.md and
    passes -p @<that-file> to claude.
    """
    existing_files = [p.name for p in list_phase_files(task.folder)]
    existing_section = "\n".join(f"- {name}" for name in existing_files) or "- (none yet)"

    correction_block = ""
    if correction_text:
        correction_block = (
            "## USER INTERVENTION — READ THIS BEFORE ANYTHING ELSE\n\n"
            "The user pressed Ctrl+C mid-session and typed the text below.\n"
            "Their instruction supersedes everything else in TASK.md and\n"
            "prior phase files for this session only. Follow it first.\n\n"
            "-----------------------------------------------------------------\n"
            f"{correction_text.strip()}\n"
            "-----------------------------------------------------------------\n\n"
        )

    prompt = f"""{correction_block}You are running inside SimpleHarness, a baton-pass agent harness.

## Where you are
- Worksite (the code/text you work on): {task.state.worksite}
- Toolbox (your brain, role files, workflows): {toolbox}
- Current task folder: {task.folder}
- Your role: {role.name}
- Workflow: {workflow.name} (phases: {" -> ".join(workflow.phases)})
- Your base model: Opus. You MUST delegate mechanical work to Sonnet/Haiku
  subagents via the Agent tool to preserve your context window.

## Files that exist in this task folder
- TASK.md (user's brief, read only)
- STATE.md (you may Edit: status, phase, next_role, blocked_reason ONLY)
{existing_section}

## What you must produce this session
- Your own phase file (e.g., 0X-{role.name.replace("-", "_")}.md or similar):
  a concise record of what you did, decisions, files touched, subagents
  dispatched, results synthesized.
- Actual changes in the worksite (code, prose, whatever the task calls for).
- STATE.md updated narrowly: status, phase, next_role, blocked_reason only.
  Use Edit (not Write) to preserve the other fields the harness manages.
- Git commits in the worksite with clear messages when your work is a
  logical unit.

## Subagent delegation (READ THIS)
You are running on Opus — expensive context. BEFORE doing heavy reading or
mechanical work yourself, dispatch an Agent subagent:

- Haiku subagent for: file search, reading multiple files to extract info,
  listing directories, git status/log/diff, mechanical refactors, test runs.
- Sonnet subagent for: a specific well-scoped subtask from the plan,
  reviewing a piece of prior output against a spec, drafting prose sections.

Use the Agent tool with model="haiku" or model="sonnet". Give each subagent
a self-contained prompt — it does NOT see this conversation. Synthesize its
result into your own phase file.

Your own Opus context is for: decisions, synthesis, judgment, orchestration.

## Boundaries
- Stay inside the worksite and this task folder.
- You may READ the toolbox for reference.
- You may NOT edit files outside the worksite UNLESS your role explicitly
  says you can (project-leader is the only privileged role).
- If you get stuck or confused: set STATE.status=blocked with a clear
  blocked_reason and STOP. Do not spin in circles.

## Your task
Read TASK.md and any existing phase files in this folder, then do your job
as described in your role instructions.
"""
    return prompt


def _build_allowlist(role: Role, config: Config) -> str:
    """Construct the --allowedTools value shared by safe and approver modes."""
    tools = DEFAULT_TOOLS_ALLOW + role.allowed_tools + config.permissions.extra_tools_allow
    bash_patterns = DEFAULT_BASH_ALLOW + config.permissions.extra_bash_allow
    seen: set[str] = set()
    dedup_tools: list[str] = []
    for t in tools:
        if t not in seen:
            seen.add(t)
            dedup_tools.append(t)
    return ",".join(dedup_tools + [f"Bash({p})" for p in bash_patterns])


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


def build_claude_cmd(
    prompt_file: Path,
    role: Role,
    toolbox: Path,
    session_id: str,
    config: Config,
    *,
    task: Task | None = None,
    jsonl_log: Path | None = None,
) -> list[str]:
    """Assemble the full `claude` command line for a single session.

    `task` and `jsonl_log` are required when `config.permissions.mode` is
    `approver` — they locate the task dir for the settings + allowlist
    files, and the jsonl log is re-exported via env for the slow-path
    hook to tail.
    """
    cmd: list[str] = [
        "claude",
        "-p",
        f"@{prompt_file}",
        "--append-system-prompt-file",
        str(toolbox / "roles" / f"{role.name}.md"),
        "--add-dir",
        str(toolbox),
        "--output-format",
        "stream-json",
        "--verbose",
        "--max-turns",
        str(role.max_turns or config.max_turns_default),
        "--session-id",
        session_id,
    ]
    if config.include_partial_messages:
        cmd.append("--include-partial-messages")

    mode = config.permissions.mode
    if mode == "dangerous":
        cmd += ["--permission-mode", "bypassPermissions"]
    elif mode == "approver":
        if task is None or jsonl_log is None:
            raise ValueError("approver mode requires task and jsonl_log arguments")
        cmd += ["--permission-mode", "acceptEdits"]
        cmd += ["--allowedTools", _build_allowlist(role, config)]
        # Seed the fast-path allowlist file so the bash PreToolUse hook
        # has the merged default + worksite patterns on the first call,
        # before the Python slow path has a chance to refresh it.
        bash_patterns = list(DEFAULT_BASH_ALLOW) + list(config.permissions.extra_bash_allow)
        write_approver_allowlist(task.folder, bash_patterns)
        settings_path = _write_approver_settings(task.folder)
        cmd += ["--settings", str(settings_path)]
    else:
        cmd += ["--permission-mode", "acceptEdits"]
        cmd += ["--allowedTools", _build_allowlist(role, config)]

    return cmd


def _popen_kwargs_windows() -> dict[str, Any]:
    """Windows-specific: CREATE_NEW_PROCESS_GROUP so Ctrl+C stays in the parent."""
    if sys.platform != "win32":
        return {}
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}


def _format_tool_call(tname: str, tinput: dict[str, Any]) -> str:
    """Return a short human-readable summary of a tool_use block's input.

    Per-tool formatting so the stream reads like a log of actions instead of
    a JSON dump.
    """
    if tname == "Bash":
        cmd = str(tinput.get("command", "")).strip()
        return f"$ {cmd}"
    if tname == "Read":
        return str(tinput.get("file_path", ""))
    if tname in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        return str(tinput.get("file_path", ""))
    if tname in ("Glob", "Grep"):
        pattern = str(tinput.get("pattern", ""))
        path = str(tinput.get("path", ""))
        return f"{pattern}  [{path}]" if path else pattern
    if tname == "Agent":
        model = str(tinput.get("model", "?"))
        desc = str(tinput.get("description", "") or tinput.get("prompt", ""))
        return f"[{model}] {desc[:80]}"
    return json.dumps(tinput, ensure_ascii=False)[:200]


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9\s-]", "", text).strip().lower()
    s = re.sub(r"[\s_-]+", "-", s)
    return s[:60] or "task"
