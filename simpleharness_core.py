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
    include_partial_messages: bool = False
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


class _FileLock:
    """Cross-platform exclusive lock via sibling .lock file.

    Uses os.open with O_CREAT | O_EXCL, which is atomic on both Windows and
    POSIX filesystems. Spins with short sleeps until acquired.
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
                self._fd = os.open(
                    str(self.lock_path),
                    os.O_CREAT | os.O_EXCL | os.O_RDWR,
                )
                return self
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"could not acquire lock {self.lock_path} within {self.timeout}s"
                    ) from None
                time.sleep(self.poll)

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.lock_path)


def append_approved_pattern(worksite: Path, pattern: str) -> None:
    """Append `pattern` to <worksite>/simpleharness/config.yaml under
    permissions.extra_bash_allow. Idempotent: no-op if already present.

    Guarded by a cross-platform file lock and written atomically via a
    temp-file + os.replace.
    """
    sh_dir = worksite / "simpleharness"
    sh_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = sh_dir / "config.yaml"

    with _FileLock(cfg_path):
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
