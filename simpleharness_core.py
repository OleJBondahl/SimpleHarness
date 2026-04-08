"""Shared loader + config helpers for SimpleHarness.

Extracted from harness.py so the approver MCP server (and any future
side-processes) can import them without pulling in the subprocess /
streaming machinery.
"""

from __future__ import annotations

import contextlib
import os
import re
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
