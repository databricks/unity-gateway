"""Tests for skills shipped with Unity Gateway."""

import json
import shlex
from pathlib import Path

import pytest

from ucode import claude_answer_delivery, skills
from ucode.constants import (
    ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR,
    ENABLE_SMART_ROUTER_OWNERSHIP_DECISION_ENV_VAR,
    ENABLE_SUBAGENT_ROUTING_ENV_VAR,
)
from ucode.smart_routing import orchestrator, ownership, ownership_transport, session_env


def _write_skill(root: Path, name: str, content: str = "version one") -> Path:
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(content)
    references = skill / "references"
    references.mkdir()
    (references / "details.md").write_text("details")
    return skill


@pytest.fixture
def bundled_skill(tmp_path, monkeypatch):
    source = tmp_path / "source"
    skill = _write_skill(source, skills.SMART_ROUTER_SKILL)
    monkeypatch.setattr(skills, "_skills_source", lambda: source)
    return skill, tmp_path / "home"


@pytest.mark.parametrize(
    ("agent", "root"), [("claude", ".claude/skills"), ("codex", ".codex/skills")]
)
@pytest.mark.parametrize("agent_specific", [False, True])
def test_copies_named_skill_to_agent_directory(bundled_skill, agent, root, agent_specific):
    source_skill, home = bundled_skill
    _write_skill(source_skill.parent, "second-skill")
    if agent_specific:
        source_skill.joinpath("SKILL.md").unlink()
        for name in ("claude", "codex"):
            source_skill.joinpath(f"SKILL.{name}.md").write_text(f"{name} instructions")

    installed = skills.install_skill(skills.SMART_ROUTER_SKILL, agent, home)

    assert installed == home / root / "smart-router"
    expected = f"{agent} instructions" if agent_specific else "version one"
    assert installed.joinpath("SKILL.md").read_text() == expected
    assert installed.joinpath("references/details.md").read_text() == "details"
    assert not home.joinpath(root, "second-skill").exists()


@pytest.mark.parametrize("entrypoint", ["SKILL.md", "SKILL.codex.md"])
def test_reinstall_replaces_changed_bundle_and_skips_unchanged_bundle(
    bundled_skill, monkeypatch, entrypoint
):
    source_skill, home = bundled_skill
    source_skill.joinpath(entrypoint).write_text("version one")
    installed = skills.install_skill(skills.SMART_ROUTER_SKILL, "codex", home)

    installed.joinpath("stale.txt").write_text("remove me")
    source_skill.joinpath(entrypoint).write_text("version two")

    installed = skills.install_skill(skills.SMART_ROUTER_SKILL, "codex", home)

    assert installed.joinpath("SKILL.md").read_text() == "version two"
    assert not installed.joinpath("stale.txt").exists()
    assert list(installed.parent.iterdir()) == [installed]

    def fail_copy(*_args, **_kwargs):
        pytest.fail("unchanged skills should not be replaced")

    monkeypatch.setattr(skills.shutil, "copytree", fail_copy)

    installed_again = skills.install_skill(skills.SMART_ROUTER_SKILL, "codex", home)

    assert installed_again == installed


def test_ownership_removes_only_the_unchanged_managed_orchestrator_skill(
    bundled_skill, monkeypatch
):
    source_skill, home = bundled_skill
    source = _write_skill(source_skill.parent, skills.SMART_ROUTER_ORCHESTRATOR_SKILL)
    source.joinpath("SKILL.claude.md").write_text("managed Claude workflow")
    installed = skills.install_skill(skills.SMART_ROUTER_ORCHESTRATOR_SKILL, "claude", home)

    assert (
        skills.remove_managed_agent_skill_if_unmodified(
            skills.SMART_ROUTER_ORCHESTRATOR_SKILL, "claude", home
        )
        is True
    )
    assert not installed.exists()
    assert (
        skills.remove_managed_agent_skill_if_unmodified(
            skills.SMART_ROUTER_ORCHESTRATOR_SKILL, "claude", home
        )
        is None
    )

    installed = skills.install_skill(skills.SMART_ROUTER_ORCHESTRATOR_SKILL, "claude", home)
    installed.joinpath("SKILL.md").write_text("custom workflow")
    assert (
        skills.remove_managed_agent_skill_if_unmodified(
            skills.SMART_ROUTER_ORCHESTRATOR_SKILL, "claude", home
        )
        is False
    )
    assert installed.joinpath("SKILL.md").read_text() == "custom workflow"


def test_ownership_uses_policy_roles_once_and_restores_bundle_state_after_compaction(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv(ENABLE_SMART_ROUTER_OWNERSHIP_DECISION_ENV_VAR, "1")
    monkeypatch.setenv(ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR, "1")
    monkeypatch.setenv(ENABLE_SUBAGENT_ROUTING_ENV_VAR, "1")
    session_path = session_env.start_session()
    (session_path.parent / "claude-answer-delivery").touch()

    plugin_dir = tmp_path / "plugin"
    orchestrator.add_claude_agents(plugin_dir)
    role_dir = plugin_dir / "agents"
    bundled_roles = orchestrator.skill_directory() / "agents"
    expected_roles = {"explorer", "researcher", "worker", "tester", "reviewer"}
    assert {path.stem for path in role_dir.glob("*.md")} == expected_roles
    for role in expected_roles:
        assert (role_dir / f"{role}.md").read_bytes() == (bundled_roles / f"{role}.md").read_bytes()

    state = {
        "schema_version": 1,
        "session_id": "native-session-1",
        "generation": 2,
        "current": {
            "request_id": "request-2",
            "input_sha256": "a" * 64,
            "policy_version": ownership.POLICY_VERSION,
            "policy_sha256": ownership.policy_sha256(),
            "decision_status": "ok",
            "ownership": "subagent",
            "dispatch": "spawn_subagent",
            "owner_agent_id": None,
            "turn_id": "turn-2",
            "agent_tool_use_id": "tool-use-2",
            "dispatch_status": "started",
        },
        "owners": [{"agent_id": "native-agent-1", "scope": "implementation", "state": "running"}],
    }
    ownership.write_state("native-session-1", state)
    settings = {}
    orchestrator.sync_hooks(settings, agent="claude")
    assert set(settings["hooks"]) == {"SessionStart"}
    assert "UserPromptSubmit" not in settings["hooks"]
    assert orchestrator.hook_output({"hook_event_name": "UserPromptSubmit"}, agent="claude") is None

    output = orchestrator.hook_output(
        {
            "hook_event_name": "SessionStart",
            "source": "compact",
            "session_id": "native-session-1",
        },
        agent="claude",
    )
    context = output["hookSpecificOutput"]["additionalContext"]
    policy = ownership.load_policy()
    assert policy["parent_instructions"] in context
    assert "Restored native ownership state (generation 2)" in context
    action_line = context.split("Validated ownership decision (runtime JSON):\n", 1)[
        1
    ].splitlines()[0]
    assert json.loads(action_line) == {
        "request_id": "request-2",
        "decision_status": "ok",
        "ownership": "subagent",
        "dispatch": "spawn_subagent",
        "owner_agent_id": None,
        "dispatch_status": "started",
    }
    assert "agent_id=native-agent-1, scope=implementation, state=running" in context
    assert claude_answer_delivery.AVAILABLE_CONTEXT in context
    assert context.count("claude_answer_delivery:") == 1
    assert "Skill directory" not in context


def test_ownership_compaction_respects_routing_off(monkeypatch):
    monkeypatch.setenv(ENABLE_SMART_ROUTER_OWNERSHIP_DECISION_ENV_VAR, "1")
    monkeypatch.setenv(ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR, "1")
    monkeypatch.setenv(ENABLE_SUBAGENT_ROUTING_ENV_VAR, "1")
    session_env.start_session()
    session_env.set_session_environment({ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0"})

    assert orchestrator.ownership_decision_enabled(agent="claude")
    assert not orchestrator.enabled(agent="claude")
    output = orchestrator.hook_output(
        {"hook_event_name": "SessionStart", "source": "compact"}, agent="claude"
    )

    assert output["hookSpecificOutput"]["additionalContext"] == orchestrator.DISABLED_CONTEXT


def test_ownership_compaction_without_a_request_adds_no_context(monkeypatch, tmp_path):
    monkeypatch.setenv(ENABLE_SMART_ROUTER_OWNERSHIP_DECISION_ENV_VAR, "1")
    monkeypatch.setenv(ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR, "1")
    monkeypatch.setenv(ENABLE_SUBAGENT_ROUTING_ENV_VAR, "1")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-profile"))
    session_env.start_session()

    output = orchestrator.hook_output(
        {"hook_event_name": "SessionStart", "source": "compact", "session_id": "new-session"},
        agent="claude",
    )

    assert output is None


def test_ownership_compaction_restores_pending_intent_marker(monkeypatch, tmp_path):
    monkeypatch.setenv(ENABLE_SMART_ROUTER_OWNERSHIP_DECISION_ENV_VAR, "1")
    monkeypatch.setenv(ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR, "1")
    monkeypatch.setenv(ENABLE_SUBAGENT_ROUTING_ENV_VAR, "1")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-profile"))
    session_env.start_session()
    request = {
        "schema_version": 1,
        "policy_version": ownership.POLICY_VERSION,
        "request_id": "pending-compaction-request",
        "task": {
            "prompt": "Inspect this function.",
            "context": [],
            "context_complete": False,
            "attachments": [],
        },
        "constraints": {
            "root_session": True,
            "subagents_allowed": True,
            "externally_assigned": False,
            "routing_enabled": True,
            "answer_delivery": True,
            "available_tools": ["Agent", "Read"],
        },
        "existing_owners": [],
    }
    prepared = ownership_transport.prepare_request(request, "pending-compaction-session")
    ownership.write_state(
        "pending-compaction-session",
        {
            "schema_version": 1,
            "session_id": "pending-compaction-session",
            "generation": 1,
            "current": {
                "request_id": request["request_id"],
                "input_sha256": None,
                "policy_version": ownership.POLICY_VERSION,
                "policy_sha256": ownership.policy_sha256(),
                "decision_status": "undecided",
                "ownership": None,
                "dispatch": None,
                "owner_agent_id": None,
                "dispatch_status": "undecided",
            },
            "owners": [],
        },
    )

    output = orchestrator.hook_output(
        {
            "hook_event_name": "SessionStart",
            "source": "compact",
            "session_id": "pending-compaction-session",
        },
        agent="claude",
    )

    assert output["hookSpecificOutput"]["additionalContext"] == prepared["instructions"]


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize(
    "payload",
    [
        {"hook_event_name": "UserPromptSubmit"},
        {"hook_event_name": "SessionStart", "source": "compact"},
    ],
)
def test_orchestrator_injects_the_installed_agent_instructions(
    bundled_skill, monkeypatch, agent, payload
):
    source_skill, home = bundled_skill
    source = _write_skill(source_skill.parent, skills.SMART_ROUTER_ORCHESTRATOR_SKILL)
    source.joinpath("SKILL.md").unlink()
    for name in ("claude", "codex"):
        source.joinpath(f"SKILL.{name}.md").write_text(f"{name} instructions")
    installed = skills.install_skill(skills.SMART_ROUTER_ORCHESTRATOR_SKILL, agent, home)
    monkeypatch.setenv("ENABLE_SMART_ROUTER_ORCHESTRATOR", "1")
    monkeypatch.setenv("ENABLE_SMART_ROUTING_SUBAGENT_ONLY", "1")
    session_env.start_session()

    settings = {}
    orchestrator.sync_hooks(settings, agent=agent)
    hook = settings["hooks"][payload["hook_event_name"]][0]["hooks"][0]
    assert shlex.split(hook["command"])[-2:] == ["--agent", agent]
    output = orchestrator.hook_output(payload, agent=agent)

    assert output is not None
    context = output["hookSpecificOutput"]["additionalContext"]
    assert context.endswith(f"\n\n{agent} instructions")
    assert context.endswith(installed.joinpath("SKILL.md").read_text())

    session_env.set_session_environment({"ENABLE_SMART_ROUTING_SUBAGENT_ONLY": "0"})
    output = orchestrator.hook_output(payload, agent=agent)
    assert output["hookSpecificOutput"]["additionalContext"] == orchestrator.DISABLED_CONTEXT
    session_env.set_session_environment({})
    output = orchestrator.hook_output(payload, agent=agent)
    assert output["hookSpecificOutput"]["additionalContext"] == context


def test_uninstalls_one_skill_from_agent_roots(bundled_skill):
    source_skill, home = bundled_skill
    _write_skill(source_skill.parent, "second-skill")
    skills.install_skill(skills.SMART_ROUTER_SKILL, "claude", home)
    skills.install_skill(skills.SMART_ROUTER_SKILL, "codex", home)
    skills.install_skill("second-skill", "codex", home)

    removed = skills.uninstall_skill(skills.SMART_ROUTER_SKILL, home)

    assert removed == [
        home / ".claude/skills/smart-router",
        home / ".codex/skills/smart-router",
    ]
    assert all(not path.exists() for path in removed)
    assert home.joinpath(".codex/skills/second-skill/SKILL.md").is_file()
    assert skills.uninstall_skill(skills.SMART_ROUTER_SKILL, home) == []


def test_uninstall_unlinks_all_global_aliases_without_removing_targets(tmp_path):
    home = tmp_path / "home"
    aliases = [
        home / root / skills.SMART_ROUTER_SKILL
        for root in (".claude/skills", ".codex/skills", ".agents/skills")
    ]
    targets = [tmp_path / "targets" / str(index) for index in range(len(aliases))]
    for target in targets[:-1]:
        target.mkdir(parents=True)
    for alias, target in zip(aliases, targets, strict=True):
        alias.parent.mkdir(parents=True)
        alias.symlink_to(target)

    removed = skills.uninstall_skill(skills.SMART_ROUTER_SKILL, home)

    assert removed == aliases
    assert all(not alias.is_symlink() for alias in aliases)
    assert all(target.is_dir() for target in targets[:-1])
    assert not targets[-1].exists()


def test_rejects_invalid_install_requests(tmp_path):
    with pytest.raises(ValueError, match="Invalid skill name"):
        skills.install_skill("../smart-router", "codex", tmp_path)
    with pytest.raises(ValueError, match="Invalid skill name"):
        skills.uninstall_skill("../smart-router", tmp_path)
    with pytest.raises(ValueError, match="Unsupported skill agent"):
        skills.install_skill(skills.SMART_ROUTER_SKILL, "other", tmp_path)
