"""SimpleHarness - a lightweight baton-pass agent harness over the Claude Code CLI.

CLI entry point and tick loop. Reads markdown role and workflow definitions from
the toolbox repo, scans a worksite's simpleharness/ folder for tasks, and runs
headless `claude -p` sessions one at a time, passing state between them via
STATE.md and per-phase markdown files on disk.

See README.md and the design plan for full architecture notes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import yaml

from simpleharness.benchmark import (
    discover_benchmark_repos,
    format_summary,
    run_all_benchmarks,
    write_results,
)
from simpleharness.core import (
    Config,
    State,
    Task,
    TaskSpec,
    Workflow,
    _slugify,
    build_rebrief_text,
    build_refinement_text,
    check_deliverables,
    classify_cli_error,
    compute_post_session_state,
    format_task_dashboard,
    parse_task_spec,
    pause_file_path,
    plan_downstream_transitions,
    plan_tick,
    resolve_next_role,
    toolbox_root,
    worksite_sh_dir,
)
from simpleharness.io import (
    load_config,
    load_role,
    load_workflow,
    now_iso,
    read_frontmatter_file,
    read_state,
    state_hash,
    write_state,
)
from simpleharness.session import run_session
from simpleharness.ui import console, err, say, warn

# ────────────────────────────────────────────────────────────────────────────
# Version + constants
# ────────────────────────────────────────────────────────────────────────────

VERSION = "0.1.0"


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
        spec = None
        if task_md.exists():
            try:
                fm, _body = read_frontmatter_file(task_md)
                spec = parse_task_spec(fm)
            except (ValueError, yaml.YAMLError) as e:
                warn(f"{child.name}: unreadable TASK.md frontmatter ({e}), skipping spec")
        out.append(
            Task(
                slug=child.name,
                folder=child,
                task_md=task_md,
                state_path=state_path,
                state=state,
                spec=spec,
            )
        )
    return out


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


def _handle_downstream_transitions(
    done_task: Task,
    all_tasks: tuple[Task, ...],
    worksite: Path,
) -> None:
    """After a task completes, update downstream tasks based on their refine_on_deps_complete."""
    all_specs: dict[str, TaskSpec] = {}
    for t in all_tasks:
        if t.spec is not None:
            all_specs[t.slug] = t.spec

    actions = plan_downstream_transitions(done_task.slug, all_specs)
    for action in actions:
        downstream = next((t for t in all_tasks if t.slug == action.task_slug), None)
        if downstream is None:
            continue
        if action.action == "block_for_rebrief":
            new_state = replace(
                downstream.state,
                status="blocked",
                blocked_reason="awaiting_brief_refinement",
            )
            write_state(downstream.state_path, new_state)
            rebrief_text = build_rebrief_text(
                done_task.slug, action.task_slug, action.upstream_deliverables
            )
            (downstream.folder / "NEEDS_REBRIEF.md").write_text(rebrief_text, encoding="utf-8")
            say(f"task {action.task_slug}: blocked for rebrief (upstream {done_task.slug} done)")
        else:
            # leave_active — write signal file for project-leader to consume
            refinement_text = build_refinement_text(done_task.slug, action.upstream_deliverables)
            (downstream.folder / "NEEDS_REFINEMENT.md").write_text(
                refinement_text, encoding="utf-8"
            )
            say(f"task {action.task_slug}: upstream {done_task.slug} done, auto-refine on next run")


def _update_blocked_tasks_index(worksite: Path) -> None:
    """Maintain simpleharness/BLOCKED_TASKS.md — written when tasks are blocked, deleted otherwise."""
    blocked = [t for t in discover_tasks(worksite) if t.state.status == "blocked"]
    index_path = worksite_sh_dir(worksite) / "BLOCKED_TASKS.md"
    if not blocked:
        if index_path.exists():
            index_path.unlink()
        return
    rows = "\n".join(f"| {t.slug} | {t.state.blocked_reason or ''} |" for t in blocked)
    index_path.write_text(
        f"# Blocked tasks\n\n| Task | Reason |\n|------|--------|\n{rows}\n",
        encoding="utf-8",
    )


def _extract_error_text(jsonl_path: Path) -> str:
    """Extract error messages from a session's .jsonl log (I/O)."""
    errors: list[str] = []
    try:
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("type") == "error":
                error_obj = event.get("error", {})
                msg = (
                    error_obj.get("message", "") if isinstance(error_obj, dict) else str(error_obj)
                )
                if msg:
                    errors.append(msg)
    except OSError:
        pass
    return "\n".join(errors)


def tick_once(worksite: Path, config: Config) -> bool:
    """One iteration of the loop. Returns True if we ran a session, False if idle."""
    tasks = tuple(discover_tasks(worksite))
    corrections = frozenset(t.slug for t in tasks if (t.folder / "CORRECTION.md").exists())
    workflows_by_name: dict[str, Workflow | None] = {
        t.state.workflow: _try_load_workflow(t.state.workflow) for t in tasks
    }
    plan = plan_tick(tasks, workflows_by_name, corrections, config, datetime.now(UTC))

    match plan.kind:
        case "no_tasks":
            say("no tasks in simpleharness/tasks/", style="dim")
            return False
        case "no_active":
            say("no active tasks", style="dim")
            return False
        case "all_backoff":
            say("all active tasks in backoff, waiting for retry window", style="dim")
            return False
        case "waiting_on_deps":
            waiting = [
                t for t in tasks if t.state.status == "active" and t.spec and t.spec.depends_on
            ]
            slugs = ", ".join(t.slug for t in waiting[:3])
            say(f"waiting on dependencies: {slugs}", style="dim")
            return False
        case "block":
            task = _task_by_slug(tasks, plan.block_task_slug)
            err(f"task {task.slug}: {plan.block_reason}")
            new_state = replace(task.state, status="blocked", blocked_reason=plan.block_reason)
            write_state(task.state_path, new_state)
            _update_blocked_tasks_index(worksite)
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
                session = run_session(task, role, workflow, config, worksite)
            except KeyboardInterrupt:
                say("aborted by user, exiting")
                raise

            post_hash = state_hash(task.state_path)
            current_state = read_state(task.state_path)

            # ── classify CLI errors for retry/backoff ────────────────────
            classify_result = None
            if not session.completed and not session.interrupted and session.exit_code != 0:
                log_root = worksite_sh_dir(worksite) / "logs" / task.slug
                jsonl_files = sorted(log_root.glob("*.jsonl")) if log_root.exists() else []
                jsonl_log = jsonl_files[-1] if jsonl_files else log_root / "missing.jsonl"
                error_text = _extract_error_text(jsonl_log)
                classify_result = classify_cli_error(session.exit_code, error_text)
                say(
                    f"task {task.slug}: CLI error classified as {classify_result.outcome}"
                    f" — {classify_result.reason}",
                    style="yellow",
                )

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
                classify_result=classify_result,
            )

            # Warn only on the tick that first crosses the threshold, not every tick after.
            if (
                new_state.no_progress_ticks >= config.no_progress_tick_threshold
                and new_state.no_progress_ticks > current_state.no_progress_ticks
            ):
                warn(f"task {task.slug}: no progress for {new_state.no_progress_ticks} ticks")

            # Deliverable verification before allowing done
            if new_state.status == "done" and task.spec and task.spec.deliverables:
                existing = frozenset(
                    d.path for d in task.spec.deliverables if (worksite / d.path).exists()
                )
                line_counts = {
                    d.path: len((worksite / d.path).read_text(encoding="utf-8").splitlines())
                    for d in task.spec.deliverables
                    if (worksite / d.path).exists()
                }
                missing = check_deliverables(task.spec, existing, line_counts)
                if missing:
                    missing_list = ", ".join(missing)
                    warn(f"task {task.slug}: missing deliverables: {missing_list}")
                    # Retry: dispatch project-leader to investigate
                    new_state = replace(
                        new_state,
                        status="active",
                        next_role="project-leader",
                        blocked_reason=f"missing deliverables (retry): {missing_list}",
                    )

            write_state(task.state_path, new_state)
            say(
                f"task {task.slug}: session complete  "
                f"(status={new_state.status}, next_role={new_state.next_role or 'auto'})"
            )

            # Downstream transitions when task completes
            if new_state.status == "done":
                _handle_downstream_transitions(task, tasks, worksite)

            _update_blocked_tasks_index(worksite)
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
        "depends_on": [],
        "deliverables": [],
        "refine_on_deps_complete": False,
        "references": [],
    }
    task_body = (
        "# Goal\n\n"
        "<describe the desired outcome in one paragraph>\n\n"
        "## Success criteria\n\n"
        "- [ ] <objectively testable criterion>\n"
        "- [ ] <objectively testable criterion>\n\n"
        "## Boundaries\n\n"
        "- <what this task must NOT touch>\n\n"
        "## Autonomy\n\n"
        "**Pre-authorized (decide and proceed):**\n"
        "- <decisions the agent can make without asking>\n\n"
        "**Must block (stop and write BLOCKED.md):**\n"
        "- <decisions that require user input>\n\n"
        "## Handoff\n\n"
        "<only needed if this task has dependents — describe what downstream consumes>\n\n"
        "## Notes\n\n"
        "<optional context>\n"
    )
    yaml_fm = yaml.safe_dump(task_frontmatter, sort_keys=False, allow_unicode=True)
    (folder / "TASK.md").write_text(f"---\n{yaml_fm}---\n\n{task_body}", encoding="utf-8")

    # STATE.md
    state = State(
        task_slug=slug,
        workflow=args.workflow,
        worksite=".",
        toolbox=".",
        status="active",
        phase="kickoff",
        next_role=None,
        last_role=None,
        total_sessions=0,
        session_cap=20,
        created=now_iso(),
        updated=now_iso(),
        total_cost_usd=0.0,
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
            f"starting watch loop (idle sleep = {config.idle_sleep_seconds}s). Ctrl+C to stop after current tick."
        )
        while True:
            if pause_file_path(worksite).exists():
                say("paused — run `simpleharness resume` to continue")
                time.sleep(config.idle_sleep_seconds)
                continue
            did_work = tick_once(worksite, config)
            if not did_work:
                time.sleep(config.idle_sleep_seconds)
    except KeyboardInterrupt:
        say("stopped by user")
        return 0


def cmd_status(args: argparse.Namespace) -> int:
    from rich.table import Table

    worksite = worksite_root(args)
    tasks = discover_tasks(worksite)
    if not tasks:
        say("no tasks found")
        return 0

    table = Table(title="SimpleHarness Tasks", show_lines=True)
    table.add_column("Task", style="cyan", no_wrap=True)
    table.add_column("Status", style="bold")
    table.add_column("Phase Progress")
    table.add_column("Sessions", justify="right")
    table.add_column("Cost", justify="right")
    table.add_column("Info")

    for t in tasks:
        workflow_phases: tuple[str, ...] = ()
        try:
            wf = load_workflow(t.state.workflow)
            workflow_phases = wf.phases
        except Exception:
            pass

        dash = format_task_dashboard(t.state, workflow_phases)

        status_style = {
            "active": "green",
            "done": "dim",
            "blocked": "yellow",
            "paused": "blue",
        }.get(dash["status"], "")
        status_text = f"[{status_style}]{dash['status']}[/]" if status_style else dash["status"]

        info = ""
        if t.state.blocked_reason:
            info = f"[yellow]{t.state.blocked_reason}[/]"
        elif t.state.next_role:
            info = f"next: {t.state.next_role}"

        table.add_row(
            t.slug,
            status_text,
            dash["phase_progress"],
            dash["sessions"],
            dash["cost"],
            info,
        )

    console.print(table)
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
    console.print(f"worksite: {worksite_root(args)}")
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


def cmd_pause(args: argparse.Namespace) -> int:
    worksite = worksite_root(args)
    pf = pause_file_path(worksite)
    if pf.exists():
        say("already paused")
        return 0
    pf.write_text("paused\n", encoding="utf-8")
    say("paused — the watch loop will idle until you run `simpleharness resume`")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    worksite = worksite_root(args)
    pf = pause_file_path(worksite)
    if not pf.exists():
        say("not paused")
        return 0
    pf.unlink()
    say("resumed — next tick will run normally")
    return 0


def cmd_benchmark_run(args: argparse.Namespace) -> int:
    benchmarks_dir = Path(args.benchmarks_dir).resolve()
    if not benchmarks_dir.exists():
        err(f"benchmarks directory not found: {benchmarks_dir}")
        return 1

    repos = discover_benchmark_repos(benchmarks_dir)
    if not repos:
        err("no benchmark repos found")
        return 1

    say(f"Running {len(repos)} benchmark task(s)...", style="bold")
    run = run_all_benchmarks(benchmarks_dir)

    # Write results
    results_dir = benchmarks_dir / "results"
    result_path = write_results(run, results_dir)
    say(f"Results written to {result_path}", style="green")

    # Print summary
    say(format_summary(run))
    return 0


def cmd_benchmark_analyze(args: argparse.Namespace) -> int:
    # For now, just a stub that prints guidance
    say("Benchmark analysis not yet integrated with harness sessions.", style="yellow")
    say("To analyze manually, review the latest results in benchmarks/results/")
    # TODO: spawn analyst role session with latest results + traces
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
    if (tb / "core.py").exists():
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

    pa = sub.add_parser("pause", parents=[common], help="Pause the watch loop")
    pa.set_defaults(func=cmd_pause)

    re_ = sub.add_parser("resume", parents=[common], help="Resume a paused watch loop")
    re_.set_defaults(func=cmd_resume)

    # Benchmark commands
    p_bench = sub.add_parser("benchmark", help="run benchmark suite and analyze results")
    bench_sub = p_bench.add_subparsers(dest="bench_action")

    p_bench_run = bench_sub.add_parser("run", help="run all benchmark tasks and score results")
    p_bench_run.add_argument(
        "--benchmarks-dir",
        metavar="PATH",
        default="benchmarks",
        help="path to benchmarks directory (default: benchmarks/)",
    )
    p_bench_run.set_defaults(func=cmd_benchmark_run)

    p_bench_analyze = bench_sub.add_parser(
        "analyze", help="analyze results and propose improvements"
    )
    p_bench_analyze.add_argument(
        "--results",
        metavar="PATH",
        help="path to benchmark-results.json (default: latest in benchmarks/results/)",
    )
    p_bench_analyze.set_defaults(func=cmd_benchmark_analyze)

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
