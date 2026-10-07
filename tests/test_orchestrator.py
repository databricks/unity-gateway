"""Orchestration follows the same launch eligibility and live state as routing."""

import copy
import io
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ucode import cli, skills
from ucode.smart_routing import orchestrator, routing, session_env, v2


@pytest.fixture
def routed_session(monkeypatch):
    monkeypatch.setenv(v2.ENABLE_SMART_ROUTING_ENV_VAR, "1")
    monkeypatch.delenv(v2.ENABLE_SUBAGENT_ROUTING_ENV_VAR, raising=False)
    monkeypatch.delenv("ISAAC_LAUNCH_MODE", raising=False)
    monkeypatch.setattr(skills, "_skills_source", lambda: Path(__file__).parents[1] / "skills")
    return session_env.start_session()


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_toggle_supersedes_workflow_and_reenables_it(routed_session, agent):
    payload = {"hook_event_name": "UserPromptSubmit"}
    workflow = orchestrator.hook_output(payload)["hookSpecificOutput"]["additionalContext"]
    assert "Apply the UG model-orchestrator workflow" in workflow
    runner = CliRunner()

    off = runner.invoke(cli.app, [agent, "--disable-smart-routing"])
    assert off.exit_code == 0, off.output
    assert "do not start new automatic" in " ".join(off.output.split())
    assert not v2.smart_routing_enabled(session_env.effective_environment())
    assert not orchestrator.enabled()
    assert orchestrator.hook_output(payload)["hookSpecificOutput"]["additionalContext"] == (
        orchestrator.DISABLED_CONTEXT
    )
    with pytest.raises(ValueError, match="supersedes any earlier"):
        orchestrator.require_enabled()

    on = runner.invoke(cli.app, [agent, "--enable-smart-routing"])
    assert on.exit_code == 0, on.output
    assert "Automatic orchestration is on" in on.output
    assert v2.smart_routing_enabled(session_env.effective_environment())
    assert orchestrator.hook_output(payload)["hookSpecificOutput"]["additionalContext"] == workflow


@pytest.mark.parametrize("mode", ["no-session", "missing-session", "off", "omni"])
def test_retained_skill_cannot_enable_orchestration(routed_session, monkeypatch, mode):
    if mode == "no-session":
        monkeypatch.delenv(session_env.SESSION_ENV_VAR)
    elif mode == "missing-session":
        routed_session.unlink()
    elif mode == "off":
        session_env.set_session_environment(dict.fromkeys(v2.SMART_ROUTING_ENV_KEYS, "0"))
    else:
        monkeypatch.setenv("ISAAC_LAUNCH_MODE", " OMNI ")
    assert (orchestrator.skill_directory() / "SKILL.md").is_file()
    assert not orchestrator.enabled()
    with pytest.raises(ValueError, match="do not start new automatic delegation"):
        orchestrator.require_enabled()


@pytest.mark.parametrize(
    "flags",
    [
        {v2.ENABLE_SMART_ROUTING_ENV_VAR: "1"},
        {v2.ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1"},
        {v2.ENABLE_SMART_ROUTING_ENV_VAR: "0", v2.ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1"},
        {v2.ENABLE_SMART_ROUTING_ENV_VAR: "0"},
        {},
    ],
)
def test_full_and_subagent_routing_share_the_gate(routed_session, flags):
    env = {session_env.SESSION_ENV_VAR: str(routed_session), **flags}
    assert orchestrator.enabled(env) == v2.smart_routing_enabled(
        session_env.effective_environment(env)
    )


def test_nested_launch_does_not_inherit_eligibility(routed_session):
    before = os.environ[session_env.SESSION_ENV_VAR]
    with session_env.fresh_launch():
        assert session_env.SESSION_PYTHON_ENV_VAR not in os.environ
        assert not orchestrator.enabled()
        # A newly eligible launch creates its own session instead of sharing the parent's toggle.
        child_session = session_env.start_session()
        assert child_session != routed_session
        assert orchestrator.enabled()
        session_env.set_session_environment(dict.fromkeys(v2.SMART_ROUTING_ENV_KEYS, "0"))
    assert os.environ[session_env.SESSION_ENV_VAR] == before
    assert orchestrator.enabled()


@pytest.mark.parametrize(
    "payload,active",
    [
        ({"hook_event_name": "UserPromptSubmit"}, True),
        ({"hook_event_name": "UserPromptSubmit", "agent_type": "custom-root"}, True),
        ({"hook_event_name": "SessionStart", "source": "compact"}, True),
        ({"hook_event_name": "SessionStart", "source": "startup"}, False),
        ({"hook_event_name": "UserPromptSubmit", "agent_id": "child"}, False),
        ({"hook_event_name": "SessionStart", "source": "compact", "agent_id": "child"}, False),
        ({"hook_event_name": []}, False),
        ({"hook_event_name": "Unknown"}, False),
        ([], False),
        (None, False),
    ],
)
def test_only_root_prompt_and_compaction_load_workflow(routed_session, payload, active):
    output = orchestrator.hook_output(payload)
    if not active:
        assert output is None
        return
    context = output["hookSpecificOutput"]["additionalContext"]
    directory = orchestrator.skill_directory()
    assert str(directory) in context
    assert context.endswith((directory / "SKILL.md").read_text())


@pytest.mark.parametrize("payload", ["", "{", "null", "[]", '"text"', "{}"])
def test_hook_entry_point_ignores_invalid_payload(monkeypatch, capsys, payload):
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    orchestrator.main()
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("state", ["missing", "directory", "invalid-encoding"])
def test_unreadable_workflow_is_not_injected(routed_session, tmp_path, monkeypatch, state):
    monkeypatch.setattr(orchestrator, "skill_directory", lambda: tmp_path)
    skill = tmp_path / "SKILL.md"
    if state == "directory":
        skill.mkdir()
    elif state == "invalid-encoding":
        skill.write_bytes(b"\xff")
    assert orchestrator.hook_output({"hook_event_name": "UserPromptSubmit"}) is None


def test_hooks_preserve_user_handlers_and_replace_only_ug_handlers(monkeypatch):
    monkeypatch.setattr(sys, "executable", "/launch installation/bin/python")
    user = {"hooks": [{"type": "command", "command": "user-policy"}]}
    doc = {
        "hooks": {
            "UserPromptSubmit": [copy.deepcopy(user)],
            "SessionStart": [copy.deepcopy(user)],
            "PreToolUse": [copy.deepcopy(user)],
        }
    }
    orchestrator.sync_hooks(doc, agent="codex")
    first = copy.deepcopy(doc)
    orchestrator.sync_hooks(doc, agent="codex")
    assert doc == first
    assert doc["hooks"]["PreToolUse"] == [user]
    for event in ("UserPromptSubmit", "SessionStart"):
        groups = doc["hooks"][event]
        assert len(groups) == 2
        assert groups[0] == user
        hook = groups[1]["hooks"][0]
        assert shlex.split(hook["command"]) == [sys.executable, "-m", orchestrator.HOOK_MODULE]
        assert hook["command_windows"] == subprocess.list2cmdline(
            [sys.executable, "-m", orchestrator.HOOK_MODULE]
        )
    assert doc["hooks"]["SessionStart"][1]["matcher"] == "compact"


def test_codex_launch_merges_prompt_and_compaction_hooks(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text(
        '[[hooks.UserPromptSubmit]]\n[[hooks.UserPromptSubmit.hooks]]\ncommand = "user-prompt"\n'
        '[[hooks.SessionStart]]\nmatcher = "compact"\n'
        '[[hooks.SessionStart.hooks]]\ncommand = "user-compact"\n'
    )
    before = config.read_bytes()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    hooks = v2._v2_hooks(
        {"workspace": "https://example.com"}, ["gpt-6-sol"], v2.config_io.read_toml_safe(config)
    )
    assert hooks["UserPromptSubmit"][0]["hooks"][0]["command"] == "user-prompt"
    assert hooks["SessionStart"][0]["hooks"][0]["command"] == "user-compact"
    assert orchestrator.HOOK_MODULE in hooks["UserPromptSubmit"][1]["hooks"][0]["command"]
    assert config.read_bytes() == before


def test_routed_claude_plugin_contains_unchanged_roles(routed_session, tmp_path):
    v2._write_routed_claude_plugin(tmp_path, ["system.ai.claude-sonnet-4-6"])
    manifest = json.loads((tmp_path / ".claude-plugin/plugin.json").read_text())
    assert manifest["name"] == "ug-smart-router"
    templates = list((orchestrator.skill_directory() / "agents").glob("*.md"))
    assert {path.stem for path in templates} == {
        "explorer",
        "researcher",
        "worker",
        "tester",
        "reviewer",
    }
    for template in templates:
        assert (tmp_path / "agents" / template.name).read_bytes() == template.read_bytes()


def test_role_contract_survives_claude_model_routing(monkeypatch):
    monkeypatch.setattr(
        v2,
        "_request_claude_routing_decision",
        lambda *_args: (
            routing.RoutingDecision(
                model="system.ai.claude-sonnet-4-6", raw_model="claude-sonnet-4-6"
            ),
            None,
        ),
    )
    contract = (
        "Act as the researcher. Verify the API using the connected documentation tool. "
        "Do not edit files or delegate. Stop on auth failure. Return source links and evidence."
    )
    payload = {
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "ug-smart-router:researcher", "prompt": contract},
    }
    result = v2.route_claude_pre_tool_use(
        payload,
        workspace="https://example.com",
        token="token",
        available_models=["system.ai.claude-sonnet-4-6"],
    )
    updated = result["hookSpecificOutput"]["updatedInput"]
    assert updated["prompt"] == contract
    assert updated["subagent_type"].startswith("ug-smart-router:ucode-route-")
    assert "model" not in updated
