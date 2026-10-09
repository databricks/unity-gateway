"""Orchestrator gating, state transitions, and native harness contracts."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest
import tomlkit

from tests.integration.utils.harness import UserSession, clean_environment
from ucode import config_io, skills
from ucode.agents import claude, codex
from ucode.codex_config import codex_config_args
from ucode.constants import AGENT_CLAUDE, AGENT_CODEX, ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR
from ucode.smart_routing import orchestrator, session_env

NAME = skills.SMART_ROUTER_ORCHESTRATOR_SKILL
AGENTS = [AGENT_CLAUDE, AGENT_CODEX]


@pytest.fixture
def home(monkeypatch):
    home = config_io.APP_DIR.parent
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setattr(claude, "CLAUDE_SETTINGS_PATH", home / ".claude/ucode-settings.json")
    monkeypatch.setattr(skills, "_skills_source", lambda: Path(__file__).parents[1] / "skills")
    return home


@pytest.mark.parametrize("agent", AGENTS)
def test_flag_transitions_are_idempotent_and_preserve_other_settings(home, monkeypatch, agent):
    config_home = home / "custom" / agent
    monkeypatch.setenv(
        "CODEX_HOME" if agent == AGENT_CODEX else "CLAUDE_CONFIG_DIR", str(config_home)
    )
    marker = session_env.start_session()
    other_hook = {"hooks": [{"type": "command", "command": "user-hook"}]}
    other_skill = {"path": str(home / "other/SKILL.md"), "enabled": False}
    doc = {
        "hooks": {"UserPromptSubmit": [other_hook]},
        "skillOverrides": {"other": "off"},
        "skills": {"config": [other_skill]},
    }
    for flag in ("0", "0", "1", "1", "0"):
        monkeypatch.setenv(ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR, flag)
        orchestrator.sync_launch_config(doc, agent=agent, session_path=marker)
        installed = config_home / "skills" / NAME / "SKILL.md"
        before = copy.deepcopy(doc)
        modified = installed.stat().st_mtime_ns if installed.exists() else None
        orchestrator.sync_launch_config(doc, agent=agent, session_path=marker)
        assert doc == before
        assert (installed.stat().st_mtime_ns if installed.exists() else None) == modified
        assert doc["hooks"]["UserPromptSubmit"][0] == other_hook
        assert len(doc["hooks"]["UserPromptSubmit"]) == (2 if flag == "1" else 1)
        assert ("SessionStart" in doc["hooks"]) == (flag == "1")
        if agent == AGENT_CODEX:
            entries = doc["skills.config"]
            assert entries[0] == other_skill
            assert all(entry["enabled"] == (flag == "1") for entry in entries[1:])
            assert len(entries) == len({entry["path"] for entry in entries})
        else:
            assert doc["skillOverrides"] == {"other": "off", NAME: "on" if flag == "1" else "off"}
        # Activation depends on the orchestrator flag, even with routing off.
        output = orchestrator.hook_output({"hook_event_name": "UserPromptSubmit"}, agent=agent)
        context = output["hookSpecificOutput"]["additionalContext"]
        assert ("is on for this session" in context) == (flag == "1")
        assert ("## Workflow" in context) == (flag == "1")
        if flag == "1":
            assert installed.is_file()
            session_env.set_session_environment({ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "0"})
            output = orchestrator.hook_output(
                {"hook_event_name": "SessionStart", "source": "compact"}, agent=agent
            )
            assert (
                output["hookSpecificOutput"]["additionalContext"] == orchestrator.DISABLED_CONTEXT
            )
            session_env.set_session_environment({})
            assert orchestrator.enabled(agent=agent)


@pytest.mark.parametrize("agent", AGENTS)
def test_enabled_launch_fails_if_the_skill_cannot_be_installed(home, monkeypatch, agent):
    def fail_install(*args):
        raise OSError("read-only skill directory")

    monkeypatch.setattr(skills, "install_skill", fail_install)
    monkeypatch.setenv(ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR, "1")
    with pytest.raises(OSError, match="read-only"):
        orchestrator.sync_launch_config({}, agent=agent)
    monkeypatch.setenv(ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR, "0")
    orchestrator.sync_launch_config({}, agent=agent)


@pytest.mark.parametrize("source", ["config.toml", "ucode.config.toml"])
def test_codex_preserves_existing_skill_config_and_overrides_old_orchestrator_entries(
    home, monkeypatch, source
):
    monkeypatch.setenv(ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR, "0")
    root = home / ".codex"
    root.mkdir()
    other = {"path": str(home / "other/SKILL.md"), "enabled": False}
    previous = {"path": str(home / "legacy" / NAME / "SKILL.md"), "enabled": True}
    path = root / source
    path.write_text(tomlkit.dumps({"skills": {"config": [other, previous]}}))
    original = path.read_bytes()
    doc = {}
    orchestrator.sync_launch_config(doc, agent=AGENT_CODEX)
    assert doc["skills.config"][0] == other
    assert {"path": previous["path"], "enabled": False} in doc["skills.config"]
    assert path.read_bytes() == original


@pytest.mark.parametrize("agent", AGENTS)
def test_native_harness_gates_previously_installed_and_project_skills(home, monkeypatch, agent):
    binary = shutil.which(agent)
    if binary is None:
        pytest.skip(f"{agent} is not installed; native-agent CI runs this contract")
    session = UserSession(home / "native", home, Path(binary), home / "artifacts")
    session.env = clean_environment(home)
    session.env["ANTHROPIC_API_KEY"] = "unused"
    monkeypatch.chdir(session.cwd)
    skills.install_skill(NAME, agent, home)
    project_root = ".agents" if agent == AGENT_CODEX else ".claude"
    shutil.copytree(home / f".{agent}/skills/{NAME}", session.cwd / project_root / "skills" / NAME)
    launches = []
    monkeypatch.setattr(codex, "exec_or_spawn", launches.append)
    for flag in ("0", "1", "0"):
        monkeypatch.setenv(ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR, flag)
        session.env[ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR] = flag
        if agent == AGENT_CODEX:
            value = codex_config_args(
                {
                    "skills.config": [
                        {
                            "path": str(home / ".codex/skills" / NAME / "SKILL.md"),
                            "enabled": flag != "1",
                        }
                    ]
                }
            )[1]
            codex._run_codex(
                {}, [binary], ["--config", value, "app-server"], otel_tracing=False, workspace=None
            )
            response = session.app_server_handshake(
                launches[-1][1:],
                timeout=15,
                binary=Path(binary),
                request=("skills/list", {"cwds": [str(session.cwd)], "forceReload": True}),
            )
            own = [row for row in response["result"]["data"][0]["skills"] if row["name"] == NAME]
            assert own, response
            assert str(session.cwd / ".agents/skills" / NAME / "SKILL.md") in {
                row["path"] for row in own
            }
            assert all(row["enabled"] == (flag == "1") for row in own)
        else:
            caller = [
                "--settings",
                json.dumps({"skillOverrides": {NAME: "off" if flag == "1" else "on"}}),
            ]
            sdk_args = [
                "-p",
                "--input-format",
                "stream-json",
                "--output-format",
                "stream-json",
                "--verbose",
            ]
            argv = claude._build_claude_argv(
                binary, [*sdk_args, "--setting-sources", "user", *caller]
            )
            initialize = {
                "type": "control_request",
                "request_id": "init",
                "request": {"subtype": "initialize"},
            }
            result = session.run(*argv[1:], timeout=15, input_text=json.dumps(initialize) + "\n")
            responses = [json.loads(line) for line in result.stdout.splitlines()]
            response = next(
                row["response"] for row in responses if row.get("type") == "control_response"
            )
            assert response["subtype"] == "success", response
            names = {row["name"] for row in response["response"]["commands"]}
            assert (NAME in names) == (flag == "1")
            if flag == "0":
                argv = claude._build_claude_argv(
                    binary,
                    [
                        "-p",
                        f"/{NAME}",
                        "--output-format",
                        "json",
                        "--setting-sources",
                        "user",
                        *caller,
                    ],
                )
                result = session.run(*argv[1:], timeout=15)
                payload = json.loads(result.stdout)
                assert "skillOverrides" in payload["result"], payload
                assert payload["num_turns"] == 0
