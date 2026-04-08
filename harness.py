"""SimpleHarness - a lightweight baton-pass agent harness over the Claude Code CLI.

Single-file MVP. Reads markdown role and workflow definitions from the toolbox
repo, scans a worksite's simpleharness/ folder for tasks, and runs headless
`claude -p` sessions one at a time, passing state between them via STATE.md
and per-phase markdown files on disk.

See README.md and the design plan for full architecture notes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

# ────────────────────────────────────────────────────────────────────────────
# Version + constants
# ────────────────────────────────────────────────────────────────────────────

VERSION = "0.1.0"

# Default tool names that are always allowed in safe mode. Roles can widen
# this via their frontmatter `allowed_tools` field; users can widen it via
# config.yaml `permissions.extra_tools_allow`.
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

# Default Bash command glob patterns that are allowed in safe mode. Each entry
# is the content inside the `Bash(...)` wrapper that Claude Code uses for
# permission rules. Users extend via config.yaml `permissions.extra_bash_allow`.
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
# Dataclasses: Config, Role, Workflow, State, Task
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class Permissions:
    dangerous_auto_approve: bool = False
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
    """The toolbox repo root (where harness.py lives)."""
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
    perms = Permissions(
        dangerous_auto_approve=bool(perms_raw.get("dangerous_auto_approve", False)),
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
# Role + Workflow loading
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


def load_workflow(name: str) -> Workflow:
    path = toolbox_root() / "workflows" / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"workflow '{name}' not found at {path}")
    meta, body = read_frontmatter_file(path)
    phases = meta.get("phases") or []
    if not isinstance(phases, list) or not all(isinstance(p, str) for p in phases):
        raise ValueError(f"workflow '{name}': phases must be a list of role-name strings")
    return Workflow(
        name=str(meta.get("name", name)),
        phases=phases,
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
    yaml_body = yaml.safe_dump(ordered, sort_keys=False, default_flow_style=False, allow_unicode=True)
    path.write_text(f"---\n{yaml_body}---\n", encoding="utf-8")


def state_hash(path: Path) -> str:
    """Hash of STATE.md content — used for no-progress detection."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def worksite_sh_dir(worksite: Path) -> Path:
    return worksite / "simpleharness"


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


# ────────────────────────────────────────────────────────────────────────────
# Role resolution (hybrid: workflow default + STATE.next_role override)
# ────────────────────────────────────────────────────────────────────────────


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


# ────────────────────────────────────────────────────────────────────────────
# Session prompt builder
# ────────────────────────────────────────────────────────────────────────────


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
            "The user interrupted the previous attempt and typed the following\n"
            "correction. Follow it before doing anything else.\n\n"
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


# ────────────────────────────────────────────────────────────────────────────
# claude subprocess invocation
# ────────────────────────────────────────────────────────────────────────────


def build_claude_cmd(
    prompt_file: Path,
    role: Role,
    toolbox: Path,
    session_id: str,
    config: Config,
) -> list[str]:
    """Assemble the full `claude` command line for a single session."""
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

    if config.permissions.dangerous_auto_approve:
        cmd += ["--permission-mode", "bypassPermissions"]
    else:
        cmd += ["--permission-mode", "acceptEdits"]
        tools = DEFAULT_TOOLS_ALLOW + role.allowed_tools + config.permissions.extra_tools_allow
        bash_patterns = DEFAULT_BASH_ALLOW + config.permissions.extra_bash_allow
        # dedupe while preserving order
        seen: set[str] = set()
        dedup_tools: list[str] = []
        for t in tools:
            if t not in seen:
                seen.add(t)
                dedup_tools.append(t)
        allowlist = ",".join(dedup_tools + [f"Bash({p})" for p in bash_patterns])
        cmd += ["--allowedTools", allowlist]

    return cmd


def _popen_kwargs_windows() -> dict[str, Any]:
    """Windows-specific: CREATE_NEW_PROCESS_GROUP so Ctrl+C stays in the parent."""
    if sys.platform != "win32":
        return {}
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}


def spawn_claude(cmd: list[str], cwd: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,  # line-buffered
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


@dataclass
class SessionResult:
    completed: bool  # true if claude exited naturally
    interrupted: bool  # true if user Ctrl+C'd
    session_id: str | None
    result_text: str | None
    exit_code: int | None


def _pretty_event(event: dict[str, Any]) -> None:
    """Render a single stream-json event to the terminal using rich."""
    etype = event.get("type", "")
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
                tinput = block.get("input", {})
                # keep tool calls compact
                brief = json.dumps(tinput, ensure_ascii=False)[:200]
                console.print(rf"[magenta]  \[tool][/] {tname}  [dim]{brief}[/]", markup=True)
            elif btype == "thinking":
                console.print(r"[dim italic]  \[thinking\.\.\.][/]")
        return
    if etype == "user":
        msg = event.get("message", {})
        for block in msg.get("content", []) or []:
            if block.get("type") == "tool_result":
                content = block.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        c.get("text", "") for c in content if isinstance(c, dict)
                    )
                summary = str(content)[:300].replace("\n", " ⏎ ")
                from rich.markup import escape as _rich_escape
                console.print(rf"[green]  \[result][/] [dim]{_rich_escape(summary)}[/]")
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
            # plain-text mirror is brief
            if isinstance(event, dict) and event.get("type") == "assistant":
                for block in event.get("message", {}).get("content", []) or []:
                    if block.get("type") == "text":
                        pf.write(block.get("text", "") + "\n")
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
    prompt = build_session_prompt(task, role, workflow, toolbox, correction)
    prompt_file = write_session_prompt_file(task, prompt)

    # 3. build command
    session_id = str(uuid.uuid4())
    cmd = build_claude_cmd(prompt_file, role, toolbox, session_id, config)

    # 4. log paths
    log_root = worksite_sh_dir(Path(task.state.worksite)) / "logs" / task.slug
    idx = task.state.total_sessions  # zero-padded to 2
    stem = f"{idx:02d}-{role.name}"
    jsonl_log = log_root / f"{stem}.jsonl"
    plain_log = log_root / f"{stem}.log"

    # 5. banner
    console.rule(f"[cyan]session {idx+1}  [bold]{role.name}[/]  task={task.slug}")
    say(f"model={config.model}  session_id={session_id[:8]}  max_turns={role.max_turns or config.max_turns_default}")
    if correction:
        say("CORRECTION.md was consumed and injected into this session's prompt.", style="yellow")

    # 6. spawn + stream
    proc = spawn_claude(cmd, Path(task.state.worksite))
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


def apply_session_bookkeeping(
    task: Task, role_name: str, session: SessionResult, config: Config
) -> None:
    """Update STATE.md fields the harness owns after a session.

    Re-reads STATE from disk because the agent may have edited it mid-session.
    """
    state = read_state(task.state_path)
    state.total_sessions += 1
    state.last_role = role_name
    state.last_session_id = session.session_id
    state.updated = now_iso()
    # consecutive same-role counter
    if task.state.last_role == role_name:
        state.consecutive_same_role = task.state.consecutive_same_role + 1
    else:
        state.consecutive_same_role = 1
    # loop guards
    if state.total_sessions >= state.session_cap:
        state.status = "blocked"
        state.blocked_reason = f"session cap reached ({state.session_cap})"
    elif state.consecutive_same_role >= config.max_same_role_repeats:
        state.status = "blocked"
        state.blocked_reason = (
            f"{role_name} ran {state.consecutive_same_role} times in a row without progress"
        )
    # no-progress detection
    # (we compute hash BEFORE any write_state, so capture now)
    # handled by the caller since we need the pre-write hash; simplified here
    write_state(task.state_path, state)
    task.state = state


def tick_once(worksite: Path, config: Config) -> bool:
    """One iteration of the loop. Returns True if we ran a session, False if idle."""
    tasks = discover_tasks(worksite)
    if not tasks:
        say("no tasks in simpleharness/tasks/", style="dim")
        return False

    task = pick_next_task(tasks)
    if task is None:
        say("no active tasks", style="dim")
        return False

    # load the workflow
    try:
        workflow = load_workflow(task.state.workflow)
    except (FileNotFoundError, ValueError) as e:
        err(f"task {task.slug}: {e}")
        task.state.status = "blocked"
        task.state.blocked_reason = f"workflow load failed: {e}"
        write_state(task.state_path, task.state)
        return False

    # session cap check before spending
    if task.state.total_sessions >= task.state.session_cap:
        warn(f"task {task.slug}: session cap reached ({task.state.session_cap}), blocking")
        task.state.status = "blocked"
        task.state.blocked_reason = f"session cap reached ({task.state.session_cap})"
        write_state(task.state_path, task.state)
        return False

    # decide next role
    next_role_name = resolve_next_role(task, workflow)
    if next_role_name is None:
        say(f"task {task.slug}: past final phase and not marked done — blocking", style="yellow")
        task.state.status = "blocked"
        task.state.blocked_reason = "past final phase without status=done"
        write_state(task.state_path, task.state)
        return False

    try:
        role = load_role(next_role_name)
    except (FileNotFoundError, ValueError) as e:
        err(f"task {task.slug}: {e}")
        task.state.status = "blocked"
        task.state.blocked_reason = f"role load failed: {e}"
        write_state(task.state_path, task.state)
        return False

    # record pre-state hash for no-progress detection
    pre_hash = state_hash(task.state_path)

    # clear any stale next_role override (it's consumed by this session)
    if task.state.next_role:
        task.state.next_role = None
        write_state(task.state_path, task.state)

    # run it
    try:
        session = run_session(task, role, workflow, config)
    except KeyboardInterrupt:
        say("aborted by user, exiting")
        raise

    # apply bookkeeping
    apply_session_bookkeeping(task, role.name, session, config)

    # re-read to check agent updates
    post_state = read_state(task.state_path)
    post_hash = state_hash(task.state_path)
    if post_hash == pre_hash:
        post_state.no_progress_ticks += 1
        if post_state.no_progress_ticks >= config.no_progress_tick_threshold:
            warn(f"task {task.slug}: no progress for {post_state.no_progress_ticks} ticks")
        write_state(task.state_path, post_state)
    else:
        if post_state.no_progress_ticks != 0:
            post_state.no_progress_ticks = 0
            write_state(task.state_path, post_state)

    say(f"task {task.slug}: session complete  (status={post_state.status}, next_role={post_state.next_role or 'auto'})")
    return True


# ────────────────────────────────────────────────────────────────────────────
# CLI commands
# ────────────────────────────────────────────────────────────────────────────


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9\s-]", "", text).strip().lower()
    s = re.sub(r"[\s_-]+", "-", s)
    return s[:60] or "task"


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
        say(f"starting watch loop (idle sleep = {config.idle_sleep_seconds}s). Ctrl+C to interrupt.")
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
    if config.permissions.dangerous_auto_approve:
        warn("dangerous_auto_approve=TRUE — checking for sandbox marker")
        in_sandbox = (
            Path("/.dockerenv").exists()
            or os.environ.get("SIMPLEHARNESS_SANDBOX") == "1"
        )
        if in_sandbox:
            say("sandbox marker detected — dangerous mode allowed", style="green")
        else:
            err(
                "dangerous_auto_approve=TRUE but no sandbox marker. "
                "Watch will refuse to run unless --i-know-its-dangerous is passed."
            )
            ok = False
    else:
        say("permission mode: SAFE (acceptEdits + curated allowlist)", style="green")

    # current worksite
    sh = worksite_sh_dir(worksite)
    if sh.exists():
        say(f"worksite simpleharness/ dir: {sh}", style="green")
    else:
        warn(f"worksite simpleharness/ dir missing — run `simpleharness init`")

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
    p_new.add_argument(
        "--workflow", default="universal", help="workflow name (default: universal)"
    )
    p_new.set_defaults(func=cmd_new)

    p_watch = sub.add_parser("watch", parents=[common], help="long-lived loop (primary mode)")
    p_watch.add_argument("--once", action="store_true", help="do one tick then exit")
    p_watch.add_argument(
        "--i-know-its-dangerous",
        action="store_true",
        help="override sandbox check when dangerous_auto_approve=true",
    )
    p_watch.set_defaults(func=cmd_watch)

    p_status = sub.add_parser("status", parents=[common], help="list active tasks + current phase")
    p_status.set_defaults(func=cmd_status)

    p_list = sub.add_parser("list", parents=[common], help="list all tasks")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", parents=[common], help="show details of one task")
    p_show.add_argument("slug")
    p_show.set_defaults(func=cmd_show)

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
