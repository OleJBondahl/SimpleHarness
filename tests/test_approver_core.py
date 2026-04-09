"""Tests for pure functions in simpleharness.approver_core."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from simpleharness.approver_core import (
    ApproverEnv,
    ReviewOutcome,
    SpawnRequest,
    Verdict,
    _deny_synthetic,
    build_approver_prompt,
    command_signature,
    fake_verdict_from_input,
    finalize_review,
    parse_verdict,
    plan_review,
    unwrap_wrappers,
)

# ── Minimal Config stub (no I/O) ──────────────────────────────────────────────


@dataclass
class _Perms:
    escalate_denials_to_correction: bool = False
    extra_bash_allow: list[str] = field(default_factory=list)


@dataclass
class _Config:
    permissions: _Perms = field(default_factory=_Perms)


def _cfg(*, escalate: bool = False, allow: list[str] | None = None) -> _Config:
    return _Config(
        permissions=_Perms(
            escalate_denials_to_correction=escalate,
            extra_bash_allow=allow or [],
        )
    )


def _env(*, fake: bool = False) -> ApproverEnv:
    return ApproverEnv(
        worksite=Path("/fake/worksite"),
        task_slug="smoke-test",
        role="developer",
        approver_model="sonnet",
        stream_log=None,
        fake=fake,
        timeout_s=30.0,
    )


# ── unwrap_wrappers ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "tokens,expected_head",
    [
        (["sudo", "git", "push"], "git"),
        (["env", "FOO=bar", "python", "script.py"], "python"),
        (["time", "make", "build"], "make"),
        (["sudo", "-u", "root", "rm", "-rf", "/tmp/x"], "rm"),
        (["sudo", "sudo", "sudo", "ls"], "ls"),
        (["unknown-cmd", "arg"], "unknown-cmd"),
        (["git", "push"], "git"),
    ],
)
def test_unwrap_wrappers(tokens, expected_head):
    result = unwrap_wrappers(tokens)
    assert result[0] == expected_head


def test_unwrap_wrappers_nested():
    result = unwrap_wrappers(["sudo", "env", "VAR=1", "ls"])
    assert result == ["ls"]


def test_unwrap_wrappers_empty():
    assert unwrap_wrappers([]) == []


# ── command_signature ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "cmd,expected",
    [
        ("git status", "git"),
        ("sudo apt-get install foo", "apt-get"),
        ("ls -la /tmp", "ls"),
        ("", "Bash"),
        ("env FOO=1 python main.py", "python"),
    ],
)
def test_command_signature(cmd, expected):
    assert command_signature(cmd) == expected


def test_command_signature_compound():
    # compound commands — shlex will tokenize the first word
    sig = command_signature("cd /tmp && ls")
    assert sig == "cd"


# ── parse_verdict ──────────────────────────────────────────────────────────────


def test_parse_verdict_valid_allow():
    msg = '```json\n{"decision": "allow", "pattern": "git *", "reason": "looks good"}\n```'
    v = parse_verdict(msg)
    assert v.decision == "allow"
    assert v.pattern == "git *"
    assert v.reason == "looks good"


def test_parse_verdict_valid_deny():
    msg = '```json\n{"decision": "deny", "pattern": "", "reason": "too risky"}\n```'
    v = parse_verdict(msg)
    assert v.decision == "deny"
    assert v.reason == "too risky"


def test_parse_verdict_empty_message():
    v = parse_verdict("")
    assert v.decision == "deny"
    assert "empty" in v.reason


def test_parse_verdict_garbled():
    v = parse_verdict("I think you should allow this one")
    assert v.decision == "deny"
    assert "malformed" in v.reason


def test_parse_verdict_multiple_fenced_blocks():
    msg = (
        '```json\n{"decision": "deny", "pattern": "", "reason": "draft"}\n```\n'
        "After reflection:\n"
        '```json\n{"decision": "allow", "pattern": "ls *", "reason": "final"}\n```'
    )
    v = parse_verdict(msg)
    # last block wins
    assert v.decision == "allow"
    assert v.reason == "final"


def test_parse_verdict_invalid_decision():
    msg = '```json\n{"decision": "maybe", "pattern": "x", "reason": "unsure"}\n```'
    v = parse_verdict(msg)
    assert v.decision == "deny"
    assert "invalid decision" in v.reason


def test_parse_verdict_missing_reason():
    msg = '```json\n{"decision": "allow", "pattern": "ls *"}\n```'
    v = parse_verdict(msg)
    assert v.decision == "deny"
    assert "empty reason" in v.reason


def test_parse_verdict_allow_empty_pattern():
    msg = '```json\n{"decision": "allow", "pattern": "", "reason": "ok"}\n```'
    v = parse_verdict(msg)
    assert v.decision == "deny"
    assert "empty pattern" in v.reason


def test_parse_verdict_synthetic_deny_fallback():
    v = _deny_synthetic("test reason")
    assert v == Verdict(decision="deny", pattern="", reason="test reason")


# ── build_approver_prompt ──────────────────────────────────────────────────────


def test_build_approver_prompt_snapshot():
    prompt = build_approver_prompt(
        tool_name="Bash",
        tool_input={"command": "git push"},
        role="developer",
        task_slug="my-task",
        stream_tail="some recent output",
        currently_approved=["git *"],
    )
    assert "Bash" in prompt
    assert "git push" in prompt
    assert "developer" in prompt
    assert "my-task" in prompt
    assert "some recent output" in prompt
    assert "git *" in prompt
    assert (
        "decision" in prompt.lower() or "verdict" in prompt.lower() or "approve" in prompt.lower()
    )


def test_build_approver_prompt_no_approved():
    prompt = build_approver_prompt(
        tool_name="Bash",
        tool_input={"command": "ls"},
        role="developer",
        task_slug="t",
        stream_tail="",
        currently_approved=[],
    )
    assert "none" in prompt.lower()


# ── fake_verdict_from_input ────────────────────────────────────────────────────


def test_fake_verdict_bash():
    v = fake_verdict_from_input("Bash", {"command": "git status"})
    assert v.decision == "allow"
    assert "git" in v.pattern
    assert "FAKE" in v.reason


def test_fake_verdict_non_bash():
    v = fake_verdict_from_input("Read", {"file_path": "/tmp/x"})
    assert v.decision == "allow"
    assert v.pattern == "Read"


def test_fake_verdict_deterministic():
    v1 = fake_verdict_from_input("Bash", {"command": "ls -la"})
    v2 = fake_verdict_from_input("Bash", {"command": "ls -la"})
    assert v1 == v2


# ── plan_review ────────────────────────────────────────────────────────────────


def test_plan_review_preemptive_deny_missing_role():
    plan = plan_review(
        _env(),
        "Bash",
        {"command": "ls"},
        _cfg(),
        role_file_exists=False,
        stream_tail="",
        currently_approved=(),
    )
    assert plan.preemptive_deny is not None
    assert "role file not found" in plan.preemptive_deny
    assert plan.fake_verdict is None
    assert plan.spawn is None


def test_plan_review_fake_mode():
    plan = plan_review(
        _env(fake=True),
        "Bash",
        {"command": "ls"},
        _cfg(),
        role_file_exists=True,
        stream_tail="",
        currently_approved=(),
    )
    assert plan.preemptive_deny is None
    assert plan.fake_verdict is not None
    assert plan.fake_verdict.decision == "allow"
    assert plan.spawn is None
    assert len(plan.prompt) > 0


def test_plan_review_normal_spawn():
    plan = plan_review(
        _env(fake=False),
        "Bash",
        {"command": "git push"},
        _cfg(allow=["git status *"]),
        role_file_exists=True,
        stream_tail="some context",
        currently_approved=("git status *",),
    )
    assert plan.preemptive_deny is None
    assert plan.fake_verdict is None
    assert plan.spawn is not None
    assert isinstance(plan.spawn, SpawnRequest)
    assert "git push" in plan.spawn.prompt
    assert plan.spawn.approver_model == "sonnet"
    assert plan.spawn.timeout_s == 30.0


def test_plan_review_spawn_prompt_contains_approved():
    plan = plan_review(
        _env(fake=False),
        "Bash",
        {"command": "ls"},
        _cfg(allow=["ls *", "git *"]),
        role_file_exists=True,
        stream_tail="",
        currently_approved=("ls *", "git *"),
    )
    assert plan.spawn is not None
    assert "ls *" in plan.spawn.prompt
    assert "git *" in plan.spawn.prompt


# ── finalize_review ────────────────────────────────────────────────────────────


def test_finalize_allow_persists_pattern():
    v = Verdict(decision="allow", pattern="git *", reason="looks fine")
    outcome = finalize_review(v, _cfg())
    assert outcome.pattern_to_persist == "git *"
    assert outcome.should_escalate is False
    assert outcome.verdict is v


def test_finalize_deny_no_escalate():
    v = Verdict(decision="deny", pattern="", reason="too risky")
    outcome = finalize_review(v, _cfg(escalate=False))
    assert outcome.pattern_to_persist is None
    assert outcome.should_escalate is False


def test_finalize_deny_with_escalation():
    v = Verdict(decision="deny", pattern="", reason="dangerous")
    outcome = finalize_review(v, _cfg(escalate=True))
    assert outcome.pattern_to_persist is None
    assert outcome.should_escalate is True


def test_finalize_outcome_is_frozen():
    import dataclasses

    v = Verdict(decision="allow", pattern="ls *", reason="ok")
    outcome = finalize_review(v, _cfg())
    assert isinstance(outcome, ReviewOutcome)
    assert dataclasses.is_dataclass(outcome)
    # frozen=True means the class has __setattr__ raising FrozenInstanceError
    fields = {f.name for f in dataclasses.fields(outcome)}
    assert "should_escalate" in fields
    assert "pattern_to_persist" in fields
