"""Tests for the session-wiring pure helpers in simpleharness.core
and the shell-side _write_session_settings / _write_approver_settings helpers.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from simpleharness.core import (
    Config,
    Role,
    Skill,
    SkillList,
    SkillsConfig,
    Subagent,
    build_exported_subagent_file,
    build_session_env,
    build_session_hooks_config,
    build_subagent_export_body,
    build_subagent_export_frontmatter,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _subagent(
    name: str = "test-agent",
    body: str = "Agent body.",
    *,
    description: str = "",
    model: str | None = None,
    tools: tuple[str, ...] = (),
    skills: SkillList | None = None,
) -> Subagent:
    return Subagent(
        name=name,
        body=body,
        description=description,
        model=model,
        tools=tools,
        skills=skills or SkillList(),
    )


def _role(
    name: str = "developer",
    *,
    skills: SkillList | None = None,
) -> Role:
    return Role(name=name, body="role body", skills=skills or SkillList())


def _config(
    *,
    default_must_use: tuple[str, ...] = (),
    default_available: tuple[Skill, ...] = (),
    enforcement: str = "strict",
) -> Config:
    return Config(
        skills=SkillsConfig(
            default_available=default_available,
            default_must_use=default_must_use,
            enforcement=enforcement,
        )
    )


# ---------------------------------------------------------------------------
# build_subagent_export_body
# ---------------------------------------------------------------------------


def test_export_body_empty_skills_returns_original() -> None:
    sa = _subagent(body="Original body.")
    assert build_subagent_export_body(sa) == "Original body."


def test_export_body_must_use_adds_skill_requirements_section() -> None:
    sa = _subagent(
        body="Original body.",
        skills=SkillList(must_use=("updating-memory", "commit")),
    )
    result = build_subagent_export_body(sa)
    assert result.startswith("## Skill Requirements")
    assert "updating-memory" in result
    assert "commit" in result
    # original body must still be present
    assert "Original body." in result
    # section comes before original body
    assert result.index("## Skill Requirements") < result.index("Original body.")


def test_export_body_available_only_section_present() -> None:
    sa = _subagent(
        body="Body here.",
        skills=SkillList(available=(Skill("humanizer", "strip AI tells"),)),
    )
    result = build_subagent_export_body(sa)
    assert "## Skill Requirements" in result
    assert "humanizer" in result
    assert "strip AI tells" in result
    assert "Body here." in result


def test_export_body_available_without_hint() -> None:
    sa = _subagent(
        body="B.",
        skills=SkillList(available=(Skill("commit"),)),
    )
    result = build_subagent_export_body(sa)
    assert "commit" in result


# ---------------------------------------------------------------------------
# build_subagent_export_frontmatter
# ---------------------------------------------------------------------------


def test_export_frontmatter_strips_simpleharness_fields() -> None:
    sa = _subagent(name="my-agent", description="Does stuff", tools=("Bash", "Read"))
    fm = build_subagent_export_frontmatter(sa)
    assert "privileged" not in fm
    assert "invocation" not in fm
    assert "skills" not in fm
    assert "source_path" not in fm


def test_export_frontmatter_preserves_standard_fields() -> None:
    sa = _subagent(
        name="my-agent",
        description="Does stuff",
        tools=("Bash", "Read"),
        model="haiku",
    )
    fm = build_subagent_export_frontmatter(sa)
    assert fm["name"] == "my-agent"
    assert fm["description"] == "Does stuff"
    assert "Bash" in fm["tools"]
    assert "Read" in fm["tools"]
    assert fm["model"] == "haiku"


def test_export_frontmatter_no_model_when_none() -> None:
    sa = _subagent(name="x", model=None)
    fm = build_subagent_export_frontmatter(sa)
    assert "model" not in fm


def test_export_frontmatter_no_tools_when_empty() -> None:
    sa = _subagent(name="x", tools=())
    fm = build_subagent_export_frontmatter(sa)
    assert "tools" not in fm


def test_export_frontmatter_no_description_when_empty() -> None:
    sa = _subagent(name="x", description="")
    fm = build_subagent_export_frontmatter(sa)
    assert "description" not in fm


# ---------------------------------------------------------------------------
# build_exported_subagent_file
# ---------------------------------------------------------------------------


def test_exported_file_has_frontmatter_block() -> None:
    sa = _subagent(name="code-runner", description="Runs code", body="Do things.")
    content = build_exported_subagent_file(sa)
    assert content.startswith("---\n")
    # find closing ---
    second_dashes = content.index("---\n", 4)
    assert second_dashes > 4


def test_exported_file_frontmatter_then_body() -> None:
    sa = _subagent(name="code-runner", description="Runs code", body="Do things.")
    content = build_exported_subagent_file(sa)
    # frontmatter ends with ---\n then body follows
    after_fm = content.split("---\n", 2)[-1]
    assert "Do things." in after_fm


# ---------------------------------------------------------------------------
# build_session_hooks_config
# ---------------------------------------------------------------------------

PYTHON = sys.executable


def test_hooks_config_off_returns_empty() -> None:
    result = build_session_hooks_config("off", PYTHON)
    assert result == {}


def test_hooks_config_off_preserves_existing() -> None:
    existing: dict = {"PreToolUse": [{"type": "command", "command": "bash x.sh"}]}
    result = build_session_hooks_config("off", PYTHON, existing_hooks=existing)
    assert result == existing


def test_hooks_config_strict_has_three_events() -> None:
    result = build_session_hooks_config("strict", PYTHON)
    assert "SessionStart" in result
    assert "Stop" in result
    assert "SubagentStop" in result


def test_hooks_config_strict_commands_use_python_m() -> None:
    result = build_session_hooks_config("strict", PYTHON)
    for event in ("SessionStart", "Stop", "SubagentStop"):
        hooks = result[event]
        assert any("simpleharness.hooks." in h["command"] for h in hooks)
        assert any(PYTHON in h["command"] for h in hooks)


def test_hooks_config_preserves_existing_pretooluse() -> None:
    existing: dict = {"PreToolUse": [{"type": "command", "command": "bash approver.sh"}]}
    result = build_session_hooks_config("strict", PYTHON, existing_hooks=existing)
    assert "PreToolUse" in result
    assert result["PreToolUse"] == existing["PreToolUse"]
    assert "SessionStart" in result
    assert "Stop" in result
    assert "SubagentStop" in result


def test_hooks_config_warn_also_adds_events() -> None:
    result = build_session_hooks_config("warn", PYTHON)
    assert "SessionStart" in result
    assert "Stop" in result


# ---------------------------------------------------------------------------
# build_session_env
# ---------------------------------------------------------------------------


def test_session_env_has_all_five_keys() -> None:
    role = _role("dev")
    config = _config()
    result = build_session_env({}, role, (), config)
    assert "SIMPLEHARNESS_ROLE" in result
    assert "SIMPLEHARNESS_AVAILABLE_SKILLS" in result
    assert "SIMPLEHARNESS_MUST_USE_MAIN" in result
    assert "SIMPLEHARNESS_MUST_USE_SUB" in result
    assert "SIMPLEHARNESS_ENFORCEMENT" in result


def test_session_env_role_name() -> None:
    role = _role("my-role")
    config = _config()
    result = build_session_env({}, role, (), config)
    assert result["SIMPLEHARNESS_ROLE"] == "my-role"


def test_session_env_enforcement_value() -> None:
    role = _role()
    config = _config(enforcement="warn")
    result = build_session_env({}, role, (), config)
    assert result["SIMPLEHARNESS_ENFORCEMENT"] == "warn"


def test_session_env_merges_config_defaults_into_must_use_main() -> None:
    role = _role(skills=SkillList(must_use=("role-skill",)))
    config = _config(default_must_use=("updating-memory",))
    result = build_session_env({}, role, (), config)
    must_use = json.loads(result["SIMPLEHARNESS_MUST_USE_MAIN"])
    assert "updating-memory" in must_use
    assert "role-skill" in must_use


def test_session_env_does_not_mutate_base_env() -> None:
    base = {"EXISTING": "yes"}
    role = _role()
    config = _config()
    result = build_session_env(base, role, (), config)
    assert "EXISTING" in result
    assert "SIMPLEHARNESS_ROLE" not in base  # base unchanged


def test_session_env_subagent_must_use_sub_populated() -> None:
    sa = _subagent("coder", skills=SkillList(must_use=("commit",)))
    config = _config(default_must_use=("updating-memory",))
    role = _role()
    result = build_session_env({}, role, (sa,), config)
    must_use_sub = json.loads(result["SIMPLEHARNESS_MUST_USE_SUB"])
    assert "coder" in must_use_sub
    # subagent picks up config default
    assert "updating-memory" in must_use_sub["coder"]
    assert "commit" in must_use_sub["coder"]


def test_session_env_available_skills_json_format() -> None:
    role = _role(skills=SkillList(available=(Skill("humanizer", "strip AI"),)))
    config = _config()
    result = build_session_env({}, role, (), config)
    available = json.loads(result["SIMPLEHARNESS_AVAILABLE_SKILLS"])
    assert isinstance(available, list)
    assert any(s["name"] == "humanizer" for s in available)


# ---------------------------------------------------------------------------
# Shell-side: _write_session_settings and _write_approver_settings
# ---------------------------------------------------------------------------


def test_write_session_settings_enforcement_off_returns_none(tmp_path: Path) -> None:
    from simpleharness.io import _write_session_settings

    result = _write_session_settings(tmp_path, "off")
    assert result is None


def test_write_session_settings_strict_writes_file(tmp_path: Path) -> None:
    from simpleharness.io import _write_session_settings

    result = _write_session_settings(tmp_path, "strict")
    assert result is not None
    assert result.exists()
    data = json.loads(result.read_text())
    assert "hooks" in data
    hooks = data["hooks"]
    assert "SessionStart" in hooks
    assert "Stop" in hooks
    assert "SubagentStop" in hooks


def test_write_approver_settings_includes_pretooluse(tmp_path: Path) -> None:
    from simpleharness.io import _write_approver_settings

    result = _write_approver_settings(tmp_path, "off")
    data = json.loads(result.read_text())
    assert "PreToolUse" in data["hooks"]


def test_write_approver_settings_strict_includes_skill_hooks(tmp_path: Path) -> None:
    from simpleharness.io import _write_approver_settings

    result = _write_approver_settings(tmp_path, "strict")
    data = json.loads(result.read_text())
    hooks = data["hooks"]
    assert "PreToolUse" in hooks
    assert "SessionStart" in hooks
    assert "Stop" in hooks
    assert "SubagentStop" in hooks
