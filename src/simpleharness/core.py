"""Pure dataclasses, constants, and pure functions for SimpleHarness.

Contains ONLY pure code: dataclasses, constants, and functions with no
file I/O, subprocess calls, or environment reads. All impure helpers
(file loading, locking, allowlist writing) live in shell.py.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import deal
import yaml

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

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
    """Tool permission settings for a harness session."""

    mode: str = "safe"
    approver_model: str = "sonnet"
    escalate_denials_to_correction: bool = False
    extra_bash_allow: tuple[str, ...] = field(default_factory=tuple)
    extra_tools_allow: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Skill:
    """A named skill with an optional usage hint."""

    name: str
    hint: str = ""


@dataclass(frozen=True)
class SkillList:
    """Available and required skills for a role or subagent."""

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
class OllamaConfig:
    """Connection settings for a local Ollama instance."""

    base_url: str = "http://localhost:11434"
    default_model: str = "qwen3.5"


@dataclass(frozen=True)
class Config:
    """Top-level harness configuration loaded from config.yml."""

    model: str = "opus"
    idle_sleep_seconds: int = 30
    max_sessions_per_task: int = 20
    max_same_role_repeats: int = 3
    no_progress_tick_threshold: int = 5
    max_turns_default: int = 60
    include_partial_messages: bool = True
    permissions: Permissions = field(default_factory=Permissions)
    skills: SkillsConfig = field(default_factory=SkillsConfig)
    ollama: OllamaConfig = field(default_factory=OllamaConfig)


@dataclass(frozen=True)
class Role:
    """A named agent role with its system prompt and metadata."""

    name: str
    body: str  # the system prompt body (frontmatter stripped)
    description: str = ""
    model: str | None = None
    provider: str | None = None  # None = subscription, "ollama" = local Ollama
    max_turns: int | None = None
    allowed_tools: tuple[str, ...] = field(default_factory=tuple)
    privileged: bool = False
    source_path: Path | None = None
    skills: SkillList = field(default_factory=SkillList)


@dataclass(frozen=True)
class Subagent:
    """A named subagent definition with its system prompt and tool list."""

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


@deal.chain(deal.has(), deal.raises(ValueError))
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
    result = dict(base)
    stack: list[tuple[dict[str, Any], dict[str, Any]]] = [(result, override)]
    while stack:
        target, source = stack.pop()
        for k, v in source.items():
            if isinstance(v, dict) and isinstance(target.get(k), dict):
                target[k] = dict(target[k])
                stack.append((target[k], v))
            else:
                target[k] = v
    return result


@deal.chain(deal.has(), deal.raises(ValueError))
def _parse_available_skills(available_raw: list[str | dict[str, object]]) -> tuple[Skill, ...]:
    """Parse a list of skill entries from frontmatter into a tuple of Skill objects.

    Each entry may be a plain string (name only) or a dict with a required ``name``
    key and an optional ``hint`` key. Any other type raises ``ValueError``.

    Args:
        available_raw: The raw list read from the ``skills.available`` frontmatter field.

    Returns:
        A tuple of Skill objects parsed from the list.

    Raises:
        ValueError: If any entry is not a string or dict, or if a dict entry
            is missing the ``name`` key.
    """
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
    return tuple(skills)


@deal.chain(deal.has(), deal.raises(ValueError))
def _parse_optional_str_list(raw: object, field_name: str) -> tuple[str, ...]:
    """Parse an optional list-of-strings field from frontmatter.

    Returns an empty tuple if the value is None, converts each element to
    ``str`` if the value is a list, and raises ``ValueError`` for any other type.

    Args:
        raw: The raw value read from the frontmatter field (may be None).
        field_name: The dotted frontmatter key name used in error messages.

    Returns:
        A tuple of strings, or an empty tuple when the field is absent.

    Raises:
        ValueError: If ``raw`` is not None or a list.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"{field_name}: must be a list")
    return tuple(str(s) for s in raw)


@deal.chain(deal.has(), deal.raises(ValueError))
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
        available = _parse_available_skills(available_raw)

    must_use = _parse_optional_str_list(raw.get("must_use"), "skills.must_use")
    exclude_default_must_use = _parse_optional_str_list(
        raw.get("exclude_default_must_use"), "skills.exclude_default_must_use"
    )

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
class LoopConfig:
    """Configuration for a loop phase block in a workflow."""

    roles: tuple[str, ...]
    max_cycles: int = 5
    max_critic_rounds: int = 2
    on_exhaust: str = "skip_and_flag"  # skip_and_flag | block


@dataclass(frozen=True)
class LoopState:
    """Tracks progress through a loop phase. Managed by the harness, not roles."""

    current_step: int = 0
    total_steps: int = 0
    cycle: int = 0
    critic_rounds: int = 0
    last_inner_role: str | None = None
    flagged_steps: tuple[int, ...] = ()
    inner_phase: str = "building"  # building | reviewing | critiquing | advancing | e2e_testing


@dataclass(frozen=True)
class Workflow:
    """A named workflow with an ordered list of phase names."""

    name: str
    phases: tuple[str | LoopConfig, ...]
    max_sessions: int | None = None
    idle_sleep_seconds: int | None = None
    description: str = ""
    source_path: Path | None = None


@deal.chain(deal.has(), deal.raises(ValueError))
def parse_workflow_phases(
    phases_raw: tuple[Any, ...],
) -> tuple[str | LoopConfig, ...]:
    """Parse workflow phases list, converting loop dicts to LoopConfig."""
    result: list[str | LoopConfig] = []
    for item in phases_raw:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict) and "loop" in item:
            loop_data = item["loop"]
            result.append(
                LoopConfig(
                    roles=tuple(loop_data.get("roles", ())),
                    max_cycles=int(loop_data.get("max_cycles", 5)),
                    max_critic_rounds=int(loop_data.get("max_critic_rounds", 2)),
                    on_exhaust=str(loop_data.get("on_exhaust", "skip_and_flag")),
                )
            )
        else:
            raise ValueError(f"invalid phase entry: {item!r}")
    return tuple(result)


@dataclass(frozen=True)
class State:
    """Persisted task lifecycle state written to STATE.md."""

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
    # loop phase tracking (harness-managed)
    loop_state: LoopState | None = None


@dataclass(frozen=True)
class Task:
    """A discovered task with its folder paths and current state."""

    slug: str
    folder: Path
    task_md: Path
    state_path: Path
    state: State
    spec: TaskSpec | None = None


@dataclass(frozen=True)
class SessionResult:
    """Outcome of a single Claude Code session."""

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
    """A required output file declared in a task spec."""

    path: str
    description: str = ""
    min_lines: int | None = None


@dataclass(frozen=True)
class TaskSpec:
    """Parsed task specification from TASK.md frontmatter."""

    title: str
    workflow: str
    depends_on: tuple[str, ...] = field(default_factory=tuple)
    deliverables: tuple[Deliverable, ...] = field(default_factory=tuple)
    refine_on_deps_complete: bool = False
    references: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class DownstreamAction:
    """An action to apply to a downstream task when an upstream task completes."""

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
    """Return the simpleharness config subdirectory inside a worksite."""
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
    missing = [
        d.path
        for d in spec.deliverables
        if d.path not in existing_paths
        or (d.min_lines is not None and counts.get(d.path, 0) < d.min_lines)
    ]
    return tuple(missing)


@deal.pure
def _parse_retry_after(retry_after: str) -> datetime | None:
    """Parse a retry_after ISO string, returning None on invalid input."""
    try:
        return datetime.fromisoformat(retry_after)
    except (ValueError, TypeError):
        return None


@deal.pure
def pick_next_task(
    tasks: Sequence[Task], corrections: frozenset[str], now: datetime
) -> Task | None:
    """Priority: CORRECTION.md exists > active + deps met > lowest slug.

    ``corrections`` is the pre-computed set of task slugs that have a
    CORRECTION.md on disk — the shell caller performs that I/O.

    A task is only a candidate if it is ``active`` AND its ``depends_on``
    slugs are all ``done`` (or it has no spec / no deps).

    Tasks in backoff (retry_after > now) are skipped unless they have a
    CORRECTION.md override.
    """
    all_states: dict[str, str] = {t.slug: t.state.status for t in tasks}
    candidates = [
        t
        for t in tasks
        if t.state.status == "active" and (t.spec is None or deps_satisfied(t.spec, all_states))
    ]
    # filter out tasks in backoff (corrections bypass backoff)
    candidates = [
        t
        for t in candidates
        if t.slug in corrections
        or t.state.retry_after is None
        or (_parse_retry_after(t.state.retry_after) or now) <= now
    ]
    if not candidates:
        return None
    # tasks with CORRECTION.md take priority
    with_correction = [t for t in candidates if t.slug in corrections]
    if with_correction:
        return sorted(with_correction, key=lambda t: t.slug)[0]
    return sorted(candidates, key=lambda t: t.slug)[0]


@deal.pure
def find_current_phase_index(
    phases: tuple[str | LoopConfig, ...], last_role: str | None
) -> int | None:
    """Find which phase index the task is currently in.

    For loop phases, checks if last_role is one of the loop's roles.
    Returns None if last_role not found in any phase.
    """
    if last_role is None:
        return None
    for i, phase in enumerate(phases):
        if isinstance(phase, str) and phase == last_role:
            return i
        if isinstance(phase, LoopConfig) and last_role in phase.roles:
            return i
    return None


@deal.pure
def _find_loop_config_for_role(
    phases: tuple[str | LoopConfig, ...], role_name: str | None
) -> LoopConfig | None:
    """Find the LoopConfig that contains the given role name."""
    if role_name is None:
        return None
    for phase in phases:
        if isinstance(phase, LoopConfig) and role_name in phase.roles:
            return phase
    return None


@deal.pure
def _first_role_of_phase(phase: str | LoopConfig) -> str | None:
    """Return the first role name for a phase (string or LoopConfig)."""
    if isinstance(phase, str):
        return phase
    if isinstance(phase, LoopConfig):
        return phase.roles[0] if phase.roles else None
    return None


@deal.pure
def _advance_past_index(
    phases: tuple[str | LoopConfig, ...],
    idx: int | None,
) -> str | None:
    """Return the first role of the phase after *idx*, or None if past the end."""
    if idx is None:
        return _first_role_of_phase(phases[0]) if phases else None
    if idx + 1 >= len(phases):
        return None
    return _first_role_of_phase(phases[idx + 1])


@deal.pure
def _resolve_in_loop(
    ls: LoopState,
    phases: tuple[str | LoopConfig, ...],
    last: str | None,
) -> str | None:
    """Resolve the next role when already inside a loop."""
    if ls.inner_phase == "done":
        idx = find_current_phase_index(phases, last)
        return _advance_past_index(phases, idx)
    lc = _find_loop_config_for_role(phases, last)
    if lc is not None:
        role, _new_ls = resolve_loop_role(ls, lc)
        return role
    return None


@deal.pure
def resolve_next_role(task: Task, workflow: Workflow) -> str | None:
    """Hybrid: STATE.next_role wins if set, else advance along workflow.phases.

    For loop phases, delegates to resolve_loop_role when inside a loop,
    or enters the loop when advancing to a loop phase.
    Returns None if the task is past its final phase.
    """
    if task.state.status != "active":
        return None
    if task.state.next_role:
        return task.state.next_role
    phases = workflow.phases
    if not phases:
        return None

    last = task.state.last_role

    if task.state.loop_state is not None:
        return _resolve_in_loop(task.state.loop_state, phases, last)

    if last is None:
        return _first_role_of_phase(phases[0])

    idx = find_current_phase_index(phases, last)
    if idx is None:
        return _first_role_of_phase(phases[0])

    return _advance_past_index(phases, idx)


@deal.pure
def resolve_loop_role(loop_state: LoopState, loop_config: LoopConfig) -> tuple[str, LoopState]:
    """Given current loop state, return (next_role_name, updated_loop_state).

    This dispatches the role for the current inner_phase. It does NOT
    process verdicts — those are handled by apply_review_verdict,
    apply_critique_verdict, and apply_e2e_verdict.
    """
    roles = loop_config.roles  # (builder, reviewer, critic)
    builder = roles[0]
    reviewer = roles[1]
    critic = roles[2]

    match loop_state.inner_phase:
        case "building":
            return builder, replace(loop_state, inner_phase="reviewing", last_inner_role=builder)
        case "reviewing":
            return reviewer, replace(loop_state, last_inner_role=reviewer)
        case "critiquing":
            return critic, replace(loop_state, last_inner_role=critic)
        case "e2e_testing":
            return builder, replace(loop_state, last_inner_role=builder)
        case _:
            return builder, replace(loop_state, inner_phase="building", last_inner_role=builder)


@deal.pure
def apply_review_verdict(
    loop_state: LoopState, loop_config: LoopConfig, *, verdict: str
) -> LoopState:
    """Process reviewer's pass/fail verdict and return updated loop state."""
    if verdict == "pass":
        return replace(loop_state, inner_phase="critiquing")

    # fail — increment cycle, check limits
    new_cycle = loop_state.cycle + 1
    if new_cycle >= loop_config.max_cycles:
        # exhausted retries for this step — flag and advance
        new_flagged = (*loop_state.flagged_steps, loop_state.current_step)
        next_step = loop_state.current_step + 1
        if next_step >= loop_state.total_steps:
            return replace(
                loop_state,
                flagged_steps=new_flagged,
                inner_phase="e2e_testing",
                cycle=0,
                critic_rounds=0,
            )
        return replace(
            loop_state,
            current_step=next_step,
            cycle=0,
            critic_rounds=0,
            flagged_steps=new_flagged,
            inner_phase="building",
        )
    return replace(loop_state, cycle=new_cycle, inner_phase="building")


@deal.pure
def apply_critique_verdict(
    loop_state: LoopState, loop_config: LoopConfig, *, verdict: str
) -> LoopState:
    """Process critic's approved/suggestions verdict and return updated loop state."""
    if verdict == "approved" or loop_state.critic_rounds + 1 >= loop_config.max_critic_rounds:
        # Accept step and advance
        next_step = loop_state.current_step + 1
        if next_step >= loop_state.total_steps:
            return replace(
                loop_state,
                inner_phase="e2e_testing",
                cycle=0,
                critic_rounds=0,
            )
        return replace(
            loop_state,
            current_step=next_step,
            cycle=0,
            critic_rounds=0,
            inner_phase="building",
        )
    # suggestions — loop back to builder
    return replace(
        loop_state,
        critic_rounds=loop_state.critic_rounds + 1,
        inner_phase="building",
    )


@deal.pure
def apply_e2e_verdict(loop_state: LoopState, loop_config: LoopConfig, *, verdict: str) -> LoopState:
    """Process e2e test pass/fail verdict and return updated loop state."""
    if verdict == "pass":
        return replace(loop_state, inner_phase="done")
    # fail — re-enter building
    return replace(loop_state, inner_phase="building", cycle=0, critic_rounds=0)


@deal.pure
def parse_verdict(text: str) -> str:
    """Extract verdict from a REVIEW.md or CRITIQUE.md file.

    Expected format: YAML frontmatter with a ``verdict`` field.
    Returns the verdict string, or "fail" if not found.
    """
    meta, _body = parse_frontmatter(text)
    return str(meta.get("verdict", "fail"))


@deal.pure
def _build_local_session_prompt(
    task: Task,
    role: Role,
    workflow: Workflow,
    toolbox: Path,
    correction_text: str | None,
    phase_files: list[Path],
    worksite: Path | None = None,
) -> str:
    """Minimal session prompt for local Ollama models.

    Strips subagent delegation, phase previews, and verbose instructions
    to conserve the limited context window (~8-16K tokens).
    """
    existing = ", ".join(p.name for p in phase_files) if phase_files else "(none)"

    correction = ""
    if correction_text:
        correction = f"**USER OVERRIDE:** {correction_text.strip()}\n\n"

    loop_context = ""
    if task.state.loop_state is not None:
        ls = task.state.loop_state
        if ls.inner_phase == "e2e_testing":
            loop_context = (
                "\n**E2E TESTING:** Run the full test suite. All tests must pass.\n"
                "Report results in STATE.md phase field.\n"
            )
        else:
            step_num = ls.current_step + 1  # display as 1-indexed
            loop_context = (
                f"\n**Current task:** Step {step_num} of {ls.total_steps} from PLAN.md.\n"
                f"Read '## Step {step_num}' in PLAN.md for your instructions.\n"
            )
            if ls.cycle > 0:
                loop_context += f"(Retry {ls.cycle} — previous attempt did not pass review.)\n"
            if ls.critic_rounds > 0:
                loop_context += (
                    f"(Critic round {ls.critic_rounds} — apply the suggestions from CRITIQUE.md.)\n"
                )

    return f"""{correction}You are a local coding assistant in SimpleHarness.

Worksite: {worksite or task.state.worksite}
Task folder: {task.folder}
Role: {role.name} | Phase: {task.state.phase}
Existing files: {existing}
{loop_context}
Read TASK.md and STATE.md, do your work, then update STATE.md.
Write a phase file (0X-{role.name.replace("-", "_")}.md) recording what you did.
Run each shell command in a SEPARATE Bash call. Never chain with && or ;.
If stuck, set STATE.status=blocked and stop.
"""


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
    worksite: Path | None = None,
) -> str:
    """Assemble the spatial-awareness preamble + phase instructions.

    Returns the full text. Caller writes it to <task>/.session_prompt.md and
    passes -p @<that-file> to claude.

    ``phase_files`` is the pre-computed list of existing NN-*.md phase files —
    the shell caller performs that I/O via ``list_phase_files``.
    ``phase_previews`` is an optional mapping from filename to preview text
    (first 20 lines) so agents get immediate context without extra tool calls.
    """
    # ── Local-model (Ollama) prompt: minimal tokens, direct action ──────
    if role.provider == "ollama":
        return _build_local_session_prompt(
            task,
            role,
            workflow,
            toolbox,
            correction_text,
            phase_files,
            worksite,
        )

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
- Worksite (the code/text you work on): {worksite or task.state.worksite}
- Toolbox (your brain, role files, workflows): {toolbox}
- Current task folder: {task.folder}
- Your role: {role.name}
- Workflow: {workflow.name} (phases: {" -> ".join(str(p) if isinstance(p, str) else "[loop]" for p in workflow.phases)})
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
    model = role.model or config.model
    if model:
        cmd += ["--model", model]

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
        lines.extend(f"- {name}" for name in subagent.skills.must_use)
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

    ollama_env: dict[str, str] = {}
    if role.provider == "ollama":
        ollama_env = {
            "ANTHROPIC_BASE_URL": config.ollama.base_url,
            "ANTHROPIC_AUTH_TOKEN": "ollama",
            "ANTHROPIC_API_KEY": "",
        }

    return {
        **base_env,
        **ollama_env,
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
    """Decision produced by plan_tick describing what the watch loop should do next."""

    kind: Literal["no_tasks", "no_active", "waiting_on_deps", "all_backoff", "block", "run"]
    block_task_slug: str | None = None
    block_reason: str | None = None
    run_task_slug: str | None = None
    run_role_name: str | None = None
    init_loop_state: LoopState | None = None  # set when entering a loop


@deal.pure
def _plan_when_no_task(
    tasks: tuple[Task, ...],
    now: datetime,
) -> TickPlan:
    """Determine the TickPlan when pick_next_task returned None.

    Distinguishes between all-backoff, waiting-on-deps, and no-active cases
    by inspecting the task list.

    Args:
        tasks: The full tuple of tasks (non-empty).
        now: The current wall-clock time used to evaluate backoff windows.

    Returns:
        A TickPlan with kind ``all_backoff``, ``waiting_on_deps``, or ``no_active``.
    """
    has_active_in_backoff = any(
        t.state.status == "active"
        and t.state.retry_after is not None
        and (parsed := _parse_retry_after(t.state.retry_after)) is not None
        and parsed > now
        for t in tasks
    )
    if has_active_in_backoff:
        return TickPlan(kind="all_backoff")
    has_active_with_unmet_deps = any(
        t.state.status == "active"
        and t.spec is not None
        and not deps_satisfied(t.spec, {tt.slug: tt.state.status for tt in tasks})
        for t in tasks
    )
    if has_active_with_unmet_deps:
        return TickPlan(kind="waiting_on_deps")
    return TickPlan(kind="no_active")


@deal.pure
def _resolve_next_role(
    task: Task,
    workflow: Workflow,
    corrections: frozenset[str],
) -> tuple[str, None] | tuple[None, TickPlan]:
    """Determine the next role name for a task, or return a blocking TickPlan.

    Returns a 2-tuple where either the first element is the role name (and the
    second is None), or the first element is None and the second is a blocking
    TickPlan to return immediately.

    Args:
        task: The task that is about to run.
        workflow: The workflow loaded for that task.
        corrections: The set of task slugs that have a pending correction.

    Returns:
        ``(role_name, None)`` when a role was successfully resolved, or
        ``(None, block_plan)`` when no role could be determined.
    """
    correction_pending = task.slug in corrections
    if correction_pending:
        first_phase = workflow.phases[0] if workflow.phases else None
        first_str: str | None = first_phase if isinstance(first_phase, str) else None
        role_name: str | None = task.state.last_role or first_str
        if role_name is None:
            return None, TickPlan(
                kind="block",
                block_task_slug=task.slug,
                block_reason="correction pending but workflow has no phases",
            )
        return role_name, None

    role_name = resolve_next_role(task, workflow)
    if role_name is None:
        first_phase = workflow.phases[0] if workflow.phases else None
        first_str_fb: str | None = first_phase if isinstance(first_phase, str) else None
        fallback: str | None = task.state.last_role or first_str_fb
        if fallback is None:
            return None, TickPlan(
                kind="block",
                block_task_slug=task.slug,
                block_reason="workflow has no phases",
            )
        return fallback, None
    return role_name, None


@deal.pure
def plan_tick(
    tasks: tuple[Task, ...],
    workflows_by_name: Mapping[str, Workflow | None],
    corrections: frozenset[str],
    config: Config,
    now: datetime,
) -> TickPlan:
    """Pure planner: given tasks, workflows, corrections, config → TickPlan.

    Covers all cases the old tick_once handled:
      - no_tasks: task list is empty
      - no_active: no active tasks
      - all_backoff: all active tasks are in backoff window
      - block: session cap exceeded, workflow load failure, no phases,
               correction pending but no role, etc.
      - run: a role was determined and is ready to execute
    """
    if not tasks:
        return TickPlan(kind="no_tasks")

    task = pick_next_task(tasks, corrections, now)
    if task is None:
        return _plan_when_no_task(tasks, now)

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

    next_role_name, block_plan = _resolve_next_role(task, workflow, corrections)
    if block_plan is not None:
        return block_plan

    # Check if we're entering a loop phase
    init_ls = None
    if task.state.loop_state is None:
        for phase in workflow.phases:
            if isinstance(phase, LoopConfig) and next_role_name in phase.roles:
                # Entering a loop — signal that loop_state should be initialized
                # total_steps will be set by shell.py after reading PLAN.md
                init_ls = LoopState(total_steps=0, inner_phase="building")
                break

    return TickPlan(
        kind="run",
        run_task_slug=task.slug,
        run_role_name=next_role_name,
        init_loop_state=init_ls,
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
    *,
    classify_result: ClassifyResult | None = None,
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

    # ── retry / backoff logic ────────────────────────────────────────────────
    if session.completed:
        # success → clear retry state
        new_state = replace(new_state, retry_count=0, retry_after=None)
    elif classify_result is not None and not session.interrupted:
        match classify_result.outcome:
            case "fatal":
                new_state = replace(
                    new_state,
                    status="blocked",
                    blocked_reason=classify_result.reason,
                    retry_count=0,
                    retry_after=None,
                )
            case "usage_limit":
                new_state = replace(
                    new_state,
                    retry_after=classify_result.retry_after_iso,
                )
            case "transient":
                new_retry = state.retry_count + 1
                delay = compute_backoff_delay(new_retry - 1)
                if delay is None:
                    new_state = replace(
                        new_state,
                        status="blocked",
                        blocked_reason=f"transient retries exhausted ({new_retry})",
                        retry_count=new_retry,
                        retry_after=None,
                    )
                else:
                    retry_at = (now + timedelta(seconds=delay)).strftime("%Y-%m-%dT%H:%M:%SZ")
                    new_state = replace(
                        new_state,
                        retry_count=new_retry,
                        retry_after=retry_at,
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
        r"(?:connection|request|read|connect)\s*time[d ]?\s*out",
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
        ts = m.group(1)
        if not ts.endswith("Z") and "+" not in ts:
            ts = ts + "Z"
        return ClassifyResult("usage_limit", "usage limit hit", retry_after_iso=ts)

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
    reason = last_line or f"exit code {exit_code}"
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
