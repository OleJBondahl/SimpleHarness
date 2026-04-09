"""Pure dataclasses, constants, and pure functions for SimpleHarness.

Contains ONLY pure code: dataclasses, constants, and functions with no
file I/O, subprocess calls, or environment reads. All impure helpers
(file loading, locking, allowlist writing) live in shell.py.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

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


# ────────────────────────────────────────────────────────────────────────────
# Config loading (toolbox default + worksite override)
# ────────────────────────────────────────────────────────────────────────────

# Cached at import time to avoid repeated .resolve() filesystem syscalls.
_TOOLBOX_ROOT: Path = Path(__file__).resolve().parent


def toolbox_root() -> Path:
    """The toolbox repo root (where this module lives)."""
    return _TOOLBOX_ROOT


def _merge_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_config(out[k], v)
        else:
            out[k] = v
    return out


# ────────────────────────────────────────────────────────────────────────────
# Dataclasses: Workflow, State, Task, SessionResult
# (moved from shell.py — Phase 2b)
# ────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Workflow:
    name: str
    phases: tuple[str, ...]
    max_sessions: int | None = None
    idle_sleep_seconds: int | None = None
    description: str = ""
    source_path: Path | None = None


@dataclass(frozen=True)
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


@dataclass(frozen=True)
class Task:
    slug: str
    folder: Path
    task_md: Path
    state_path: Path
    state: State


@dataclass(frozen=True)
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


def pick_next_task(tasks: Sequence[Task], corrections: frozenset[str]) -> Task | None:
    """Priority: CORRECTION.md exists > active non-blocked > lowest slug.

    ``corrections`` is the pre-computed set of task slugs that have a
    CORRECTION.md on disk — the shell caller performs that I/O.
    """
    candidates = [t for t in tasks if t.state.status == "active"]
    if not candidates:
        return None
    # tasks with CORRECTION.md take priority
    with_correction = [t for t in candidates if t.slug in corrections]
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


def build_session_prompt(
    task: Task,
    role: Role,
    workflow: Workflow,
    toolbox: Path,
    correction_text: str | None,
    phase_files: list[Path],
) -> str:
    """Assemble the spatial-awareness preamble + phase instructions.

    Returns the full text. Caller writes it to <task>/.session_prompt.md and
    passes -p @<that-file> to claude.

    ``phase_files`` is the pre-computed list of existing NN-*.md phase files —
    the shell caller performs that I/O via ``list_phase_files``.
    """
    existing_files = [p.name for p in phase_files]
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


def build_claude_cmd(
    prompt_file: Path,
    role: Role,
    toolbox: Path,
    session_id: str,
    config: Config,
    *,
    approver_settings_path: Path | None = None,
) -> list[str]:
    """Assemble the full `claude` command line for a single session.

    In approver mode, the shell caller must pre-write the allowlist and
    settings files and pass ``approver_settings_path`` pointing to the
    written settings file.
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
        cmd += ["--permission-mode", "acceptEdits"]
        cmd += ["--allowedTools", _build_allowlist(role, config)]
        if approver_settings_path is not None:
            cmd += ["--settings", str(approver_settings_path)]
    else:
        cmd += ["--permission-mode", "acceptEdits"]
        cmd += ["--allowedTools", _build_allowlist(role, config)]

    return cmd


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


# ────────────────────────────────────────────────────────────────────────────
# Functional-core: tick planner
# ────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TickPlan:
    kind: Literal["no_tasks", "no_active", "block", "run"]
    block_task_slug: str | None = None
    block_reason: str | None = None
    run_task_slug: str | None = None
    run_role_name: str | None = None


def plan_tick(
    tasks: tuple[Task, ...],
    workflows_by_name: Mapping[str, Workflow | None],
    corrections: frozenset[str],
    config: Config,
) -> TickPlan:
    """Pure planner: given tasks, workflows, corrections, config → TickPlan.

    Covers all cases the old tick_once handled:
      - no_tasks: task list is empty
      - no_active: no active tasks
      - block: session cap exceeded, workflow load failure, no phases,
               correction pending but no role, etc.
      - run: a role was determined and is ready to execute
    """
    if not tasks:
        return TickPlan(kind="no_tasks")

    task = pick_next_task(tasks, corrections)
    if task is None:
        return TickPlan(kind="no_active")

    # session cap check before spending
    if task.state.total_sessions >= task.state.session_cap:
        return TickPlan(
            kind="block",
            block_task_slug=task.slug,
            block_reason=f"session cap reached ({task.state.session_cap})",
        )

    # workflow load failure
    workflow = workflows_by_name.get(task.state.workflow)
    if workflow is None:
        return TickPlan(
            kind="block",
            block_task_slug=task.slug,
            block_reason=f"workflow load failed: {task.state.workflow!r}",
        )

    # determine next role
    correction_pending = task.slug in corrections
    if correction_pending:
        next_role_name = task.state.last_role or (workflow.phases[0] if workflow.phases else None)
        if next_role_name is None:
            return TickPlan(
                kind="block",
                block_task_slug=task.slug,
                block_reason="correction pending but workflow has no phases",
            )
    else:
        next_role_name = resolve_next_role(task, workflow)
        if next_role_name is None:
            # past final phase — loop back
            fallback = task.state.last_role or (workflow.phases[0] if workflow.phases else None)
            if fallback is None:
                return TickPlan(
                    kind="block",
                    block_task_slug=task.slug,
                    block_reason="workflow has no phases",
                )
            next_role_name = fallback

    return TickPlan(
        kind="run",
        run_task_slug=task.slug,
        run_role_name=next_role_name,
    )


# ────────────────────────────────────────────────────────────────────────────
# Functional-core: post-session state computation
# ────────────────────────────────────────────────────────────────────────────


def compute_post_session_state(
    state: State,
    role_name: str,
    session: SessionResult,
    prev_last_role: str | None,
    prev_consecutive_same_role: int,
    pre_hash: str,
    post_hash: str,
    config: Config,
    now: datetime,
) -> State:
    """Pure: compute the new State after a session completes.

    Absorbs the logic that was split between apply_session_bookkeeping and
    the no-progress counter block at the tail of tick_once.

    ``state`` is the agent-edited state re-read from disk after the session.
    ``prev_last_role`` / ``prev_consecutive_same_role`` are from the pre-session
    snapshot (before the agent ran), so consecutive-same-role counting compares
    correctly against what the harness previously recorded.
    ``pre_hash`` / ``post_hash`` are SHA-256 digests of STATE.md before and
    after the session (used for no-progress detection).
    """
    # consecutive same-role counter
    new_consecutive = prev_consecutive_same_role + 1 if prev_last_role == role_name else 1

    # no-progress detection
    new_no_progress = state.no_progress_ticks + 1 if post_hash == pre_hash else 0

    updated_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    new_state = replace(
        state,
        total_sessions=state.total_sessions + 1,
        last_role=role_name,
        last_session_id=session.session_id,
        updated=updated_iso,
        consecutive_same_role=new_consecutive,
        no_progress_ticks=new_no_progress,
    )

    # loop guards (evaluated after incrementing counters)
    if new_state.total_sessions >= new_state.session_cap:
        new_state = replace(
            new_state,
            status="blocked",
            blocked_reason=f"session cap reached ({new_state.session_cap})",
        )
    elif new_state.consecutive_same_role >= config.max_same_role_repeats:
        new_state = replace(
            new_state,
            status="blocked",
            blocked_reason=(
                f"{role_name} ran {new_state.consecutive_same_role} times in a row without progress"
            ),
        )

    return new_state
