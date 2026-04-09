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

import deal
import yaml

# ────────────────────────────────────────────────────────────────────────────
# Dataclasses: Permissions, Config, Role
# ────────────────────────────────────────────────────────────────────────────

_VALID_MODES = ("safe", "approver", "dangerous")
_VALID_APPROVER_MODELS = ("haiku", "sonnet", "opus")
_VALID_SKILL_ENFORCEMENT = ("strict", "warn", "off")


# Default Bash command glob patterns that are allowed in safe mode. Each entry
# is the content inside the ``Bash(...)`` wrapper Claude Code uses for its
# permission rules. Users extend via config.yaml ``permissions.extra_bash_allow``.
# Moved here from harness.py so the approver PreToolUse hook (which must not
# depend on harness.py) can import it.
DEFAULT_BASH_ALLOW: tuple[str, ...] = (
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
)


@dataclass(frozen=True)
class Permissions:
    mode: str = "safe"
    approver_model: str = "sonnet"
    escalate_denials_to_correction: bool = False
    extra_bash_allow: tuple[str, ...] = field(default_factory=tuple)
    extra_tools_allow: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Skill:
    name: str
    hint: str = ""


@dataclass(frozen=True)
class SkillList:
    available: tuple[Skill, ...] = field(default_factory=tuple)
    must_use: tuple[str, ...] = field(default_factory=tuple)
    exclude_default_must_use: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SkillsConfig:
    """Global skills defaults + enforcement knob.

    ``default_available`` and ``default_must_use`` are merged into every
    loaded role's own ``SkillList`` via ``merge_skill_lists``. A role can
    opt out of a specific default with ``skills.exclude_default_must_use``
    in its frontmatter.

    ``enforcement`` controls the Stop/SubagentStop hook behavior:
      - ``strict``  — missing must_use skills block the session from stopping
      - ``warn``    — missing skills are reported to stderr but don't block
      - ``off``     — the hook is not registered at all
    """

    default_available: tuple[Skill, ...] = field(default_factory=tuple)
    default_must_use: tuple[str, ...] = field(default_factory=tuple)
    enforcement: str = "strict"  # strict | warn | off


@dataclass(frozen=True)
class Config:
    model: str = "opus"
    idle_sleep_seconds: int = 30
    max_sessions_per_task: int = 20
    max_same_role_repeats: int = 3
    no_progress_tick_threshold: int = 5
    max_turns_default: int = 60
    include_partial_messages: bool = True
    permissions: Permissions = field(default_factory=Permissions)
    skills: SkillsConfig = field(default_factory=SkillsConfig)


@dataclass(frozen=True)
class Role:
    name: str
    body: str  # the system prompt body (frontmatter stripped)
    description: str = ""
    model: str | None = None
    max_turns: int | None = None
    allowed_tools: tuple[str, ...] = field(default_factory=tuple)
    privileged: bool = False
    source_path: Path | None = None
    skills: SkillList = field(default_factory=SkillList)


@dataclass(frozen=True)
class Subagent:
    name: str
    body: str
    description: str = ""
    model: str | None = None
    tools: tuple[str, ...] = field(default_factory=tuple)
    source_path: Path | None = None
    skills: SkillList = field(default_factory=SkillList)


# ────────────────────────────────────────────────────────────────────────────
# YAML frontmatter helpers
# ────────────────────────────────────────────────────────────────────────────

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


@deal.has()
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


@deal.pure
def toolbox_root() -> Path:
    """The toolbox repo root (where this module lives)."""
    return _TOOLBOX_ROOT


@deal.pure
def _merge_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_config(out[k], v)
        else:
            out[k] = v
    return out


@deal.has()
def parse_skill_list(raw: Any) -> SkillList:
    """Parse the `skills:` block from frontmatter into a SkillList.

    Accepts the raw dict (or None) read from YAML.
    Missing sub-fields become empty (zero-value defaults).
    Malformed sub-fields (wrong type, missing required keys) raise ``ValueError``
    with a message identifying the offending field.
    """
    if raw is None:
        return SkillList()
    if not isinstance(raw, dict):
        raise ValueError("skills: must be a mapping")

    available_raw = raw.get("available")
    if available_raw is None:
        available: tuple[Skill, ...] = ()
    elif not isinstance(available_raw, list):
        raise ValueError("skills.available: must be a list")
    else:
        skills: list[Skill] = []
        for entry in available_raw:
            if isinstance(entry, str):
                skills.append(Skill(name=entry))
            elif isinstance(entry, dict):
                if "name" not in entry:
                    raise ValueError("skills.available: each entry must have a 'name' key")
                skills.append(Skill(name=str(entry["name"]), hint=str(entry.get("hint", ""))))
            else:
                raise ValueError("skills.available: each entry must be a string or mapping")
        available = tuple(skills)

    must_use_raw = raw.get("must_use")
    if must_use_raw is None:
        must_use: tuple[str, ...] = ()
    elif not isinstance(must_use_raw, list):
        raise ValueError("skills.must_use: must be a list")
    else:
        must_use = tuple(str(s) for s in must_use_raw)

    exclude_raw = raw.get("exclude_default_must_use")
    if exclude_raw is None:
        exclude_default_must_use: tuple[str, ...] = ()
    elif not isinstance(exclude_raw, list):
        raise ValueError("skills.exclude_default_must_use: must be a list")
    else:
        exclude_default_must_use = tuple(str(s) for s in exclude_raw)

    return SkillList(
        available=available,
        must_use=must_use,
        exclude_default_must_use=exclude_default_must_use,
    )


@deal.pure
def merge_skill_lists(role_skills: SkillList, default_skills: SkillList) -> SkillList:
    """Merge default skills into a role's skills list.

    - `available`: union (defaults first, then role-specific; de-dup by name, role hints win).
    - `must_use`: union (defaults + role) minus anything in role.exclude_default_must_use.
      Preserves order: defaults first, then role additions, de-duplicated.
    - `exclude_default_must_use`: carried from role_skills unchanged.
    """
    # Merge available: defaults first, role wins on collision
    seen_names: dict[str, Skill] = {}
    for skill in default_skills.available:
        seen_names[skill.name] = skill
    for skill in role_skills.available:
        seen_names[skill.name] = skill  # role hint wins
    merged_available = tuple(seen_names.values())

    # Merge must_use: defaults + role, minus exclude_default_must_use
    excluded = set(role_skills.exclude_default_must_use)
    merged_must_use_names: dict[str, None] = {}
    for name in default_skills.must_use:
        if name not in excluded:
            merged_must_use_names[name] = None
    for name in role_skills.must_use:
        merged_must_use_names[name] = None
    merged_must_use = tuple(merged_must_use_names)

    return SkillList(
        available=merged_available,
        must_use=merged_must_use,
        exclude_default_must_use=role_skills.exclude_default_must_use,
    )


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
    total_cost_usd: float = 0.0
    # anti-stall
    no_progress_ticks: int = 0
    # human-facing
    blocked_reason: str | None = None
    # consecutive same-role counter (harness-managed, not spec'd in plan but needed)
    consecutive_same_role: int = 0
    # retry / backoff (harness-managed)
    retry_count: int = 0
    retry_after: str | None = None  # ISO 8601 timestamp


@dataclass(frozen=True)
class Task:
    slug: str
    folder: Path
    task_md: Path
    state_path: Path
    state: State
    spec: TaskSpec | None = None


@dataclass(frozen=True)
class SessionResult:
    completed: bool  # true if claude exited naturally
    interrupted: bool  # true if user Ctrl+C'd
    session_id: str | None
    result_text: str | None
    exit_code: int | None
    cost_usd: float | None = None
    duration_ms: int | None = None


ErrorOutcome = Literal["usage_limit", "transient", "fatal"]


@dataclass(frozen=True)
class ClassifyResult:
    """Result of classifying a CLI error."""

    outcome: ErrorOutcome
    reason: str
    retry_after_iso: str | None = None  # ISO 8601 reset timestamp (usage_limit only)


# ────────────────────────────────────────────────────────────────────────────
# Task specification (parsed from TASK.md frontmatter)
# ────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Deliverable:
    path: str
    description: str = ""
    min_lines: int | None = None


@dataclass(frozen=True)
class TaskSpec:
    title: str
    workflow: str
    depends_on: tuple[str, ...] = field(default_factory=tuple)
    deliverables: tuple[Deliverable, ...] = field(default_factory=tuple)
    refine_on_deps_complete: bool = False
    references: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class DownstreamAction:
    task_slug: str
    action: Literal["leave_active", "block_for_rebrief"]
    upstream_deliverables: tuple[Deliverable, ...]


# ────────────────────────────────────────────────────────────────────────────
# Pure helpers (moved from shell.py — Phase 2b)
# ────────────────────────────────────────────────────────────────────────────

# Default tool names that are always allowed in safe mode.
DEFAULT_TOOLS_ALLOW: tuple[str, ...] = (
    "Edit",
    "Write",
    "MultiEdit",
    "Read",
    "Glob",
    "Grep",
    "NotebookEdit",
    "Agent",
)


@deal.pure
def worksite_sh_dir(worksite: Path) -> Path:
    return worksite / "simpleharness"


@deal.pure
def pause_file_path(worksite: Path) -> Path:
    """Return the path to the pause signal file."""
    return worksite / "simpleharness" / ".PAUSE"


@deal.pure
def parse_task_spec(frontmatter: dict[str, Any]) -> TaskSpec:
    """Convert raw TASK.md frontmatter dict to a typed TaskSpec.

    Missing fields get safe defaults so old-style tasks continue to work.
    """
    raw_deps = frontmatter.get("depends_on") or []
    raw_deliverables = frontmatter.get("deliverables") or []
    raw_refs = frontmatter.get("references") or []

    deliverables = tuple(
        Deliverable(
            path=str(d["path"]),
            description=str(d.get("description", "")),
            min_lines=int(d["min_lines"]) if d.get("min_lines") is not None else None,
        )
        if isinstance(d, dict)
        else Deliverable(path=str(d))
        for d in raw_deliverables
    )

    return TaskSpec(
        title=str(frontmatter.get("title", "")),
        workflow=str(frontmatter.get("workflow", "")),
        depends_on=tuple(str(s) for s in raw_deps),
        deliverables=deliverables,
        refine_on_deps_complete=bool(frontmatter.get("refine_on_deps_complete", False)),
        references=tuple(str(r) for r in raw_refs),
    )


@deal.pure
def deps_satisfied(task_spec: TaskSpec, all_states: Mapping[str, str]) -> bool:
    """True if every slug in ``depends_on`` maps to ``"done"`` in all_states."""
    return all(all_states.get(slug) == "done" for slug in task_spec.depends_on)


@deal.pure
def plan_downstream_transitions(
    done_slug: str,
    all_specs: Mapping[str, TaskSpec],
) -> tuple[DownstreamAction, ...]:
    """Compute what to do for each downstream task when *done_slug* completes.

    Returns one ``DownstreamAction`` per task whose ``depends_on`` contains
    *done_slug*. The action is determined by the downstream task's
    ``refine_on_deps_complete`` flag.
    """
    upstream_spec = all_specs.get(done_slug)
    upstream_deliverables = upstream_spec.deliverables if upstream_spec else ()

    actions: list[DownstreamAction] = []
    for slug, spec in all_specs.items():
        if done_slug in spec.depends_on:
            action: Literal["leave_active", "block_for_rebrief"] = (
                "leave_active" if spec.refine_on_deps_complete else "block_for_rebrief"
            )
            actions.append(
                DownstreamAction(
                    task_slug=slug,
                    action=action,
                    upstream_deliverables=upstream_deliverables,
                )
            )
    return tuple(actions)


@deal.pure
def build_refinement_text(
    done_slug: str,
    upstream_deliverables: tuple[Deliverable, ...],
) -> str:
    """Build the NEEDS_REFINEMENT.md content for a downstream task."""
    deliverable_lines = "\n".join(f"- `{d.path}`: {d.description}" for d in upstream_deliverables)
    return (
        f"# Refinement available\n\n"
        f"Upstream task **{done_slug}** has completed.\n\n"
        f"## Upstream deliverables\n\n"
        f"{deliverable_lines or '(none declared)'}\n\n"
        f"## Action suggested\n\n"
        f"Review the upstream outputs and incorporate any relevant findings\n"
        f"into this task's approach. This file will be consumed automatically\n"
        f"on the next session kickoff.\n"
    )


@deal.pure
def build_rebrief_text(
    done_slug: str,
    task_slug: str,
    upstream_deliverables: tuple[Deliverable, ...],
) -> str:
    """Build the NEEDS_REBRIEF.md content for a downstream task."""
    deliverable_lines = "\n".join(f"- `{d.path}`: {d.description}" for d in upstream_deliverables)
    return (
        f"# Rebrief needed\n\n"
        f"Upstream task **{done_slug}** has completed.\n\n"
        f"## Upstream deliverables\n\n"
        f"{deliverable_lines or '(none declared)'}\n\n"
        f"## Action required\n\n"
        f"Review the upstream outputs and refine this task's TASK.md "
        f"(success criteria, references, etc.), then run "
        f"`simpleharness unblock {task_slug}`.\n"
    )


@deal.pure
def check_deliverables(
    spec: TaskSpec,
    existing_paths: frozenset[str],
    line_counts: Mapping[str, int] | None = None,
) -> tuple[str, ...]:
    """Return paths of deliverables that are missing or fail content checks.

    *existing_paths* — set of deliverable paths that exist on disk.
    *line_counts* — optional mapping of path → line count for min_lines checks.
    """
    counts = line_counts or {}
    missing: list[str] = []
    for d in spec.deliverables:
        if d.path not in existing_paths or (
            d.min_lines is not None and counts.get(d.path, 0) < d.min_lines
        ):
            missing.append(d.path)
    return tuple(missing)


@deal.pure
def pick_next_task(tasks: Sequence[Task], corrections: frozenset[str]) -> Task | None:
    """Priority: CORRECTION.md exists > active + deps met > lowest slug.

    ``corrections`` is the pre-computed set of task slugs that have a
    CORRECTION.md on disk — the shell caller performs that I/O.

    A task is only a candidate if it is ``active`` AND its ``depends_on``
    slugs are all ``done`` (or it has no spec / no deps).
    """
    all_states: dict[str, str] = {t.slug: t.state.status for t in tasks}
    candidates = [
        t
        for t in tasks
        if t.state.status == "active" and (t.spec is None or deps_satisfied(t.spec, all_states))
    ]
    if not candidates:
        return None
    # tasks with CORRECTION.md take priority
    with_correction = [t for t in candidates if t.slug in corrections]
    if with_correction:
        return sorted(with_correction, key=lambda t: t.slug)[0]
    return sorted(candidates, key=lambda t: t.slug)[0]


@deal.pure
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


@deal.pure
def build_session_prompt(
    task: Task,
    role: Role,
    workflow: Workflow,
    toolbox: Path,
    correction_text: str | None,
    phase_files: list[Path],
    phase_previews: Mapping[str, str] | None = None,
    worksite_memory_preview: str | None = None,
) -> str:
    """Assemble the spatial-awareness preamble + phase instructions.

    Returns the full text. Caller writes it to <task>/.session_prompt.md and
    passes -p @<that-file> to claude.

    ``phase_files`` is the pre-computed list of existing NN-*.md phase files —
    the shell caller performs that I/O via ``list_phase_files``.
    ``phase_previews`` is an optional mapping from filename to preview text
    (first 20 lines) so agents get immediate context without extra tool calls.
    """
    existing_files = [p.name for p in phase_files]
    if not existing_files:
        existing_section = "- (none yet)"
    else:
        previews = phase_previews or {}
        lines = []
        for name in existing_files:
            preview = previews.get(name, "")
            if preview:
                lines.append(f"### {name}\n```\n{preview}\n```")
            else:
                lines.append(f"- {name}")
        existing_section = "\n".join(lines)

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

    worksite_memory_block = ""
    if worksite_memory_preview:
        worksite_memory_block = (
            f"\n## Cross-session memory (WORKSITE.md)\n```\n{worksite_memory_preview}\n```\n"
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
{worksite_memory_block}
## What you must produce this session
- Your own phase file (e.g., 0X-{role.name.replace("-", "_")}.md or similar):
  a concise record of what you did, decisions, files touched, subagents
  dispatched, results synthesized.
- Actual changes in the worksite (code, prose, whatever the task calls for).
- STATE.md updated narrowly: status, phase, next_role, blocked_reason only.
  Use Edit (not Write) to preserve the other fields the harness manages.
- Before ending your session, commit all files you created or modified
  (phase files, STATE.md, WORKSITE.md) with a clear message referencing
  the task. Every session should leave a clean git state for the next agent.
- WORKSITE.md (simpleharness/memory/WORKSITE.md) is cross-session memory.
  Before ending your session, update it with notes for the next agent:
  what you did, what state things are in, gotchas or decisions made.
  Write it after your main work is done. Keep it concise — a handoff memo,
  not a diary.

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
- Run each shell command in a SEPARATE Bash tool call. Never chain with
  `&&`, `||`, `;`, or newlines. Never use inline Python (`python -c`) —
  write scripts to the worksite `claude-tools/` directory and run them
  with `uv run python <script>`.
- If you get stuck or confused: set STATE.status=blocked with a clear
  blocked_reason and STOP. Do not spin in circles.

## Your task
Read TASK.md and any existing phase files in this folder, then do your job
as described in your role instructions.
"""
    return prompt


@deal.pure
def _build_allowlist(role: Role, config: Config) -> str:
    """Construct the --allowedTools value shared by safe and approver modes."""
    tools = (
        DEFAULT_TOOLS_ALLOW
        + tuple(role.allowed_tools)
        + tuple(config.permissions.extra_tools_allow)
    )
    bash_patterns = DEFAULT_BASH_ALLOW + tuple(config.permissions.extra_bash_allow)
    dedup_tools = list(dict.fromkeys(tools))
    return ",".join(dedup_tools + [f"Bash({p})" for p in bash_patterns])


@deal.pure
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
    else:
        cmd += ["--permission-mode", "acceptEdits"]
        cmd += ["--allowedTools", _build_allowlist(role, config)]

    if approver_settings_path is not None:
        cmd += ["--settings", str(approver_settings_path)]

    return cmd


@deal.pure
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


@deal.pure
def build_subagent_export_body(subagent: Subagent) -> str:
    """Render the body written to <worksite>/.claude/agents/<name>.md.

    Prepends a '## Skill Requirements' section derived from subagent.skills to
    the original body so the subagent sees its required/available skills
    without needing a SessionStart hook (SessionStart doesn't fire for subagents).

    If skills.available and skills.must_use are both empty, return the original
    body unchanged.
    """
    if not subagent.skills.available and not subagent.skills.must_use:
        return subagent.body

    lines: list[str] = ["## Skill Requirements", ""]
    if subagent.skills.must_use:
        lines.append("You MUST invoke these skills before finishing:")
        for name in subagent.skills.must_use:
            lines.append(f"- {name}")
        lines.append("")
    if subagent.skills.available:
        lines.append("Available skills (invoke via the Skill tool):")
        for skill in subagent.skills.available:
            entry = f"- {skill.name}"
            if skill.hint:
                entry += f": {skill.hint}"
            lines.append(entry)
        lines.append("")
    lines.append("")
    return "\n".join(lines) + subagent.body


@deal.pure
def build_subagent_export_frontmatter(subagent: Subagent) -> dict[str, Any]:
    """Return only the fields Claude Code's .claude/agents/ format understands.

    Specifically: name, description, tools (joined comma-string), model (if set).
    Strips SimpleHarness-only fields (privileged, invocation, skills, source_path).
    """
    fm: dict[str, Any] = {"name": subagent.name}
    if subagent.description:
        fm["description"] = subagent.description
    if subagent.tools:
        fm["tools"] = ", ".join(subagent.tools)
    if subagent.model:
        fm["model"] = subagent.model
    return fm


@deal.pure
def build_exported_subagent_file(subagent: Subagent) -> str:
    """Full file contents for <worksite>/.claude/agents/<name>.md.

    Renders Claude-Code-compatible frontmatter + body (with baked skill reminder).
    """
    fm = build_subagent_export_frontmatter(subagent)
    fm_lines: list[str] = []
    for k, v in fm.items():
        fm_lines.append(f"{k}: {v}")
    frontmatter_block = "---\n" + "\n".join(fm_lines) + "\n---\n"
    body = build_subagent_export_body(subagent)
    return frontmatter_block + body


@deal.pure
def build_session_hooks_config(
    enforcement_mode: str,
    python_executable: str,
    existing_hooks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the merged 'hooks' block to write into settings JSON.

    - If enforcement_mode == 'off': no skill hooks are added. Return existing_hooks
      unchanged (or empty dict if None).
    - Otherwise: merge SessionStart, Stop, and SubagentStop entries for the
      simpleharness hook scripts into existing_hooks. Preserves any entries
      already present (e.g. the approver's PreToolUse Bash hook).
    """
    base: dict[str, Any] = dict(existing_hooks) if existing_hooks else {}
    if enforcement_mode == "off":
        return base

    def _hook(module: str) -> dict[str, Any]:
        return {"type": "command", "command": f"{python_executable} -m {module}"}

    for event, module in (
        ("SessionStart", "simpleharness.hooks.inform_skills"),
        ("Stop", "simpleharness.hooks.enforce_must_use"),
        ("SubagentStop", "simpleharness.hooks.enforce_must_use"),
    ):
        entry = [_hook(module)]
        if event in base:
            base[event] = list(base[event]) + entry
        else:
            base[event] = entry
    return base


@deal.pure
def build_session_env(
    base_env: dict[str, str],
    role: Role,
    subagents: tuple[Subagent, ...],
    config: Config,
) -> dict[str, str]:
    """Return a new env dict with SimpleHarness vars added for the child session.

    Starts from base_env, adds:
        SIMPLEHARNESS_ROLE              = role.name
        SIMPLEHARNESS_AVAILABLE_SKILLS  = JSON of merged skills.available
                                          ({name, hint} dicts)
        SIMPLEHARNESS_MUST_USE_MAIN     = JSON list of merged skills.must_use names
        SIMPLEHARNESS_MUST_USE_SUB      = JSON object mapping subagent name →
                                          merged must_use list
        SIMPLEHARNESS_ENFORCEMENT       = config.skills.enforcement

    'Merged' means: merge_skill_lists(role.skills, SkillList(
        available=config.skills.default_available,
        must_use=config.skills.default_must_use,
    )).

    Does not mutate base_env; returns a new dict.
    """
    defaults = SkillList(
        available=config.skills.default_available,
        must_use=config.skills.default_must_use,
    )
    merged_role = merge_skill_lists(role.skills, defaults)

    available_json = json.dumps([{"name": s.name, "hint": s.hint} for s in merged_role.available])
    must_use_main_json = json.dumps(list(merged_role.must_use))

    must_use_sub: dict[str, list[str]] = {}
    for sa in subagents:
        merged_sa = merge_skill_lists(sa.skills, defaults)
        must_use_sub[sa.name] = list(merged_sa.must_use)
    must_use_sub_json = json.dumps(must_use_sub)

    return {
        **base_env,
        "SIMPLEHARNESS_ROLE": role.name,
        "SIMPLEHARNESS_AVAILABLE_SKILLS": available_json,
        "SIMPLEHARNESS_MUST_USE_MAIN": must_use_main_json,
        "SIMPLEHARNESS_MUST_USE_SUB": must_use_sub_json,
        "SIMPLEHARNESS_ENFORCEMENT": config.skills.enforcement,
    }


@deal.pure
def _slugify(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9\s-]", "", text).strip().lower()
    s = re.sub(r"[\s_-]+", "-", s)
    return s[:60] or "task"


# ────────────────────────────────────────────────────────────────────────────
# Functional-core: tick planner
# ────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TickPlan:
    kind: Literal["no_tasks", "no_active", "waiting_on_deps", "block", "run"]
    block_task_slug: str | None = None
    block_reason: str | None = None
    run_task_slug: str | None = None
    run_role_name: str | None = None


@deal.pure
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
        # Distinguish: truly no active tasks vs active but deps unmet
        has_active_with_unmet_deps = any(
            t.state.status == "active"
            and t.spec is not None
            and not deps_satisfied(t.spec, {tt.slug: tt.state.status for tt in tasks})
            for t in tasks
        )
        if has_active_with_unmet_deps:
            return TickPlan(kind="waiting_on_deps")
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


@deal.pure
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
        total_cost_usd=state.total_cost_usd + (session.cost_usd or 0.0),
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


# ── CLI error classification ─────────────────────────────────────────────────

_USAGE_LIMIT_RE = re.compile(
    r"usage.limit.*reset.*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|\+\d{2}:\d{2})?)",
    re.IGNORECASE,
)

_TRANSIENT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"overloaded",
        r"\b529\b",
        r"\b503\b",
        r"rate.?limit",
        r"ECONNRESET",
        r"ETIMEDOUT",
        r"\bDNS\b",
        r"timeout",
    )
)

_AUTH_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b401\b",
        r"invalid api key",
        r"not authenticated",
        r"token expired",
    )
)


DEFAULT_BACKOFF_SCHEDULE: tuple[int, ...] = (30, 60, 120, 240, 300)


@deal.pure
def compute_backoff_delay(
    retry_count: int,
    schedule: tuple[int, ...] = DEFAULT_BACKOFF_SCHEDULE,
) -> int | None:
    """Return the backoff delay in seconds for the given retry count, or None if exhausted."""
    if retry_count >= len(schedule):
        return None
    return schedule[retry_count]


@deal.pure
def classify_cli_error(exit_code: int | None, error_text: str) -> ClassifyResult:
    """Classify a CLI error into usage_limit, transient, or fatal.

    Checks usage-limit first (has reset time), then transient patterns,
    then auth/fatal patterns. Unknown errors default to fatal.
    """
    # usage limit with reset timestamp (checked first — takes priority)
    m = _USAGE_LIMIT_RE.search(error_text)
    if m:
        return ClassifyResult("usage_limit", "usage limit hit", retry_after_iso=m.group(1))

    # transient patterns
    for pat in _TRANSIENT_PATTERNS:
        if pat.search(error_text):
            return ClassifyResult("transient", f"matched transient pattern: {pat.pattern}")

    # auth / fatal patterns
    for pat in _AUTH_PATTERNS:
        if pat.search(error_text):
            return ClassifyResult("fatal", "auth_expired — run claude login in container")

    # unknown → fatal (loud stop, not silent retry)
    last_line = error_text.strip().splitlines()[-1] if error_text.strip() else ""
    reason = last_line if last_line else f"exit code {exit_code}"
    return ClassifyResult("fatal", reason)


@deal.pure
def format_task_dashboard(
    state: State,
    workflow_phases: tuple[str, ...],
) -> dict[str, str]:
    """Build display fields for a task dashboard row.

    Returns a dict with keys: status, phase_progress, sessions, cost.
    """
    phase_parts = []
    for p in workflow_phases:
        if p == state.phase:
            phase_parts.append(f"[{p.upper()}]")
        else:
            phase_parts.append(p)
    phase_progress = " > ".join(phase_parts) if phase_parts else state.phase

    sessions = f"{state.total_sessions}/{state.session_cap}"
    cost = f"${state.total_cost_usd:.2f}" if state.total_cost_usd > 0 else "\u2014"

    return {
        "status": state.status,
        "phase_progress": phase_progress,
        "sessions": sessions,
        "cost": cost,
    }
