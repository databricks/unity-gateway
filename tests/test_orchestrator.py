"""Orchestrator gating, state transitions, and native harness contracts."""

from __future__ import annotations

import copy
import json
import os
import queue
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
import tomlkit

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
    marker = session_env.start_session()
    other_hook = {"hooks": [{"type": "command", "command": "user-hook"}]}
    other_skill = {"path": str(home / "other/SKILL.md"), "enabled": False}
    doc = {
        "hooks": {"UserPromptSubmit": [other_hook]},
        "skillOverrides": {"other": "off"},
        "skills": {"config": [other_skill]},
    }
    for flag in ("0", "1", "0", "1"):
        monkeypatch.setenv(ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR, flag)
        orchestrator.sync_launch_config(doc, agent=agent, session_path=marker)
        installed = home / f".{agent}/skills/{NAME}/SKILL.md"
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


@pytest.mark.parametrize("agent", AGENTS)
def test_session_disable_suppresses_an_old_activation_hook(home, monkeypatch, agent):
    monkeypatch.setenv(ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR, "1")
    orchestrator.sync_launch_config({}, agent=agent)
    session_env.set_session_environment({ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "0"})
    output = orchestrator.hook_output(
        {"hook_event_name": "SessionStart", "source": "compact"}, agent=agent
    )
    assert output["hookSpecificOutput"]["additionalContext"] == orchestrator.DISABLED_CONTEXT
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
def test_install_uses_the_harness_config_directory(home, tmp_path, monkeypatch, agent):
    config_home = tmp_path / "custom config home"
    monkeypatch.setenv(
        "CODEX_HOME" if agent == AGENT_CODEX else "CLAUDE_CONFIG_DIR", str(config_home)
    )
    monkeypatch.setenv(ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR, "1")
    doc = {}
    orchestrator.sync_launch_config(doc, agent=agent)
    installed = config_home / "skills" / NAME / "SKILL.md"
    assert installed.is_file()
    if agent == AGENT_CODEX:
        assert {"path": str(installed), "enabled": True} in doc["skills.config"]
    assert skills.uninstall_skill(NAME, home) == [installed.parent]
    assert not installed.exists()


@pytest.mark.parametrize(
    "option,joined",
    [("--config", False), ("--config=", True), ("-c", False), ("-c=", True), ("-c", True)],
)
@pytest.mark.parametrize("flag", ["0", "1"])
def test_codex_flag_wins_over_caller_config_and_preserves_the_prompt(
    home, monkeypatch, option, joined, flag
):
    monkeypatch.setenv(ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR, flag)
    own = home / ".codex/skills" / NAME / "SKILL.md"
    other = {"path": str(home / "other/SKILL.md"), "enabled": False}
    value = codex_config_args(
        {"skills.config": [{"path": str(own), "enabled": flag != "1"}, other]}
    )[1]
    prefix = [option + value] if joined else [option, value]
    caller = [*prefix, "--", "prompt containing --config=skills.config"]
    original = caller.copy()
    doc = {}
    args = orchestrator.codex_launch_args(caller, doc)
    assert doc["skills.config"][0] == other
    assert {"path": str(own), "enabled": flag == "1"} in doc["skills.config"]
    assert args[: len(prefix)] == prefix
    assert args[-2:] == caller[-2:]
    assert caller == original


def _codex_skills(binary, args, cwd, env):
    """Query Codex's native loader, without any inference or credentials."""
    messages = queue.Queue()
    proc = subprocess.Popen(
        [binary, *args],
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )

    def read_messages():
        for line in proc.stdout:
            messages.put(json.loads(line))

    reader = threading.Thread(target=read_messages, daemon=True)
    reader.start()
    try:

        def send(message):
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()

        def response(request_id):
            while True:
                message = messages.get(timeout=15)
                if message.get("id") == request_id:
                    assert "error" not in message, message
                    return message["result"]

        send(
            {
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "ug-test", "version": "1"}},
            }
        )
        response(1)
        send({"method": "initialized", "params": {}})
        send(
            {"id": 2, "method": "skills/list", "params": {"cwds": [str(cwd)], "forceReload": True}}
        )
        return response(2)["data"][0]["skills"]
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        reader.join(timeout=5)


@pytest.mark.parametrize("agent", AGENTS)
def test_native_harness_gates_an_installed_skill_on_repeated_launches(
    home, tmp_path, monkeypatch, agent
):
    binary = shutil.which(agent)
    if binary is None:
        pytest.skip(f"{agent} is not installed; native-agent CI runs this contract")
    # No developer auth, project config, or gateway is needed for these native APIs.
    env = {
        key: value
        for key, value in os.environ.items()
        if key in ("PATH", "SYSTEMROOT", "COMSPEC", "PATHEXT")
    }
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        CODEX_HOME=str(home / ".codex"),
        CLAUDE_CONFIG_DIR=str(home / ".claude"),
        ANTHROPIC_API_KEY="unused",
        DISABLE_AUTOUPDATER="1",
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
    )
    cwd = tmp_path / "empty project"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    skills.install_skill(NAME, agent, home)
    project_root = ".agents" if agent == AGENT_CODEX else ".claude"
    shutil.copytree(home / f".{agent}/skills/{NAME}", cwd / project_root / "skills" / NAME)
    launches = []
    if agent == AGENT_CODEX:
        monkeypatch.setattr(codex, "exec_or_spawn", launches.append)
    for flag in ("0", "0", "1", "1", "0", "1"):
        monkeypatch.setenv(ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR, flag)
        env[ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR] = flag
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
            rows = _codex_skills(binary, launches[-1][1:], cwd, env)
            own = [row for row in rows if row["name"] == NAME]
            assert own, rows
            assert str(cwd / ".agents/skills" / NAME / "SKILL.md") in {row["path"] for row in own}
            assert all(row["enabled"] == (flag == "1") for row in own)
        else:
            caller_settings = [
                "--settings",
                json.dumps({"skillOverrides": {NAME: "off" if flag == "1" else "on"}}),
            ]
            argv = claude._build_claude_argv(
                binary,
                [
                    "-p",
                    "--input-format",
                    "stream-json",
                    "--output-format",
                    "stream-json",
                    "--verbose",
                    "--setting-sources",
                    "user",
                    *caller_settings,
                ],
            )
            initialize = {
                "type": "control_request",
                "request_id": "init",
                "request": {"subtype": "initialize"},
            }
            result = subprocess.run(
                argv,
                input=json.dumps(initialize) + "\n",
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert result.returncode == 0, result.stderr
            responses = [json.loads(line) for line in result.stdout.splitlines()]
            response = next(
                row["response"] for row in responses if row.get("type") == "control_response"
            )
            assert response["subtype"] == "success", response
            names = {row["name"] for row in response["response"]["commands"]}
            assert (NAME in names) == (flag == "1")
            if flag == "0":
                # A hidden skill must also reject an explicit slash invocation.
                argv = claude._build_claude_argv(
                    binary,
                    [
                        "-p",
                        f"/{NAME}",
                        "--output-format",
                        "json",
                        "--setting-sources",
                        "user",
                        *caller_settings,
                    ],
                )
                result = subprocess.run(
                    argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=15
                )
                assert result.returncode == 0, result.stderr
                payload = json.loads(result.stdout)
                assert "skillOverrides" in payload["result"], payload
                assert payload["num_turns"] == 0
