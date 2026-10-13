"""Behavioral coverage for session-local Smart Router controls."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from ucode import cli, config_io, skills
from ucode.skills import SMART_ROUTER_SKILL
from ucode.smart_routing import session_env, v2

runner = CliRunner()


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize(
    "preset",
    [
        "subagent_only_v0",
        "first_prompt_and_subagent_no_orch_v0",
        "subagent_orch_v0_claude_only",
        None,
    ],
)
def test_session_status_tracks_toggles_without_writes(tmp_path, monkeypatch, agent, preset):
    session_file = tmp_path / "env.json"
    session_file.write_text("{}")
    monkeypatch.setenv(session_env.SESSION_ENV_VAR, str(session_file))
    if preset:
        monkeypatch.setenv("SMART_ROUTER_CONFIG_VERSION", preset)
    monkeypatch.setattr(
        cli, "_launch_tool", Mock(side_effect=AssertionError("status launched agent"))
    )
    baseline = preset is not None and not (
        preset == "subagent_orch_v0_claude_only" and agent == "codex"
    )

    for toggle, enabled in [(None, baseline), ("disable", False), ("enable", baseline)]:
        if toggle:
            assert runner.invoke(cli.app, [agent, f"--{toggle}-smart-routing"]).exit_code == 0
        before = session_file.read_bytes()
        environment = dict(os.environ)
        result = runner.invoke(cli.app, [agent, "--smart-routing-status"])
        assert result.exit_code == 0, result.output
        assert f"Smart Router is {'on' if enabled else 'off'} for this session." in result.output
        assert session_file.read_bytes() == before
        assert dict(os.environ) == environment


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize("contents", [None, "not json", '{"unknown":"1"}'])
def test_session_status_rejects_unavailable_controls(tmp_path, monkeypatch, agent, contents):
    if contents is not None:
        session_file = tmp_path / "env.json"
        session_file.write_text(contents)
        monkeypatch.setenv(session_env.SESSION_ENV_VAR, str(session_file))
    result = runner.invoke(cli.app, [agent, "--smart-routing-status"])
    assert result.exit_code == 1
    assert "Smart Router is" not in result.output
    assert "session" in result.output


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_session_status_rejects_combined_toggle(tmp_path, monkeypatch, agent):
    session_file = tmp_path / "env.json"
    session_file.write_text("{}")
    monkeypatch.setenv(session_env.SESSION_ENV_VAR, str(session_file))
    result = runner.invoke(cli.app, [agent, "--smart-routing-status", "--disable-smart-routing"])
    assert result.exit_code == 1
    assert "separately" in result.output
    assert session_file.read_text() == "{}"


@pytest.fixture(autouse=True)
def _checkout_skill_bundle(monkeypatch):
    # An editable Python install can still contain an older wheel's copied skill resources.
    monkeypatch.setattr(skills, "_skills_source", lambda: Path(__file__).parents[1] / "skills")


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_smart_routed_session_installs_skill(tmp_path, monkeypatch, agent):
    monkeypatch.delenv(session_env.SESSION_ENV_VAR, raising=False)
    monkeypatch.setenv("UCODE_SMART_ROUTER_PYTHON", "/older/install/python")

    session_path = v2._prepare_smart_router_session(agent)

    home = config_io.APP_DIR.parent
    assert home.joinpath(f".{agent}/skills/{SMART_ROUTER_SKILL}/SKILL.md").is_file()
    assert not home.joinpath(f".agents/skills/{SMART_ROUTER_SKILL}").exists()
    assert session_path == Path(os.environ[session_env.SESSION_ENV_VAR])
    assert Path(os.environ[session_env.SESSION_ENV_VAR]).is_file()
    assert os.environ["UCODE_SMART_ROUTER_PYTHON"] == sys.executable


@pytest.mark.skipif(os.name == "nt", reason="Exercises the skill's POSIX shell commands")
@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_skill_toggles_with_launch_installation_despite_shadowed_path(tmp_path, monkeypatch, agent):
    # Keep the venv path (including spaces), not its resolved system Python symlink.
    installation = tmp_path / "launch installation"
    installation.symlink_to(sys.prefix, target_is_directory=True)
    monkeypatch.setattr(sys, "executable", str(installation / "bin" / "python"))
    shadow = tmp_path / "other-installation"
    shadow.mkdir()
    other_ug = shadow / "ug"
    other_ug.write_text("#!/bin/sh\necho 'wrong ug installation' >&2\nexit 97\n")
    other_ug.chmod(0o755)
    monkeypatch.setenv("PATH", str(shadow) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv(v2.ENABLE_SUBAGENT_ROUTING_ENV_VAR, "1")

    session_path = v2._prepare_smart_router_session(agent)
    skill = config_io.APP_DIR.parent / f".{agent}/skills/{SMART_ROUTER_SKILL}/SKILL.md"
    commands = re.findall(
        r"`([^`\n]*--(?:(?:enable|disable)-smart-routing|smart-routing-status))`", skill.read_text()
    )

    for action in ("disable", "enable"):
        command = next(cmd for cmd in commands if f" {agent} --{action}-" in cmd)
        result = subprocess.run(
            ["/bin/sh", "-c", command],
            cwd=tmp_path,
            env={**os.environ, "NO_COLOR": "1"},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert f"Smart Router is {'off' if action == 'disable' else 'on'}" in result.stdout
        expected = (
            {
                v2.ENABLE_SMART_ROUTING_ENV_VAR: "0",
                v2.ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0",
                v2.ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "0",
            }
            if action == "disable"
            else {}
        )
        assert json.loads(session_path.read_text()) == expected
        status_command = next(cmd for cmd in commands if f" {agent} --smart-routing-status" in cmd)
        before = session_path.read_bytes()
        status = subprocess.run(
            ["/bin/sh", "-c", status_command],
            cwd=tmp_path,
            env={**os.environ, "NO_COLOR": "1"},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert status.returncode == 0, status.stdout + status.stderr
        assert f"Smart Router is {'off' if action == 'disable' else 'on'}" in status.stdout
        assert session_path.read_bytes() == before


def test_launcher_flags_control_routing_hook(tmp_path, monkeypatch):
    session_file = tmp_path / "env.json"
    session_file.write_text("{}")
    env = {
        session_env.SESSION_ENV_VAR: str(session_file),
        v2.ENABLE_SMART_ROUTING_ENV_VAR: "0",
        v2.ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
        "DATABRICKS_BEARER": "token",
    }
    route = Mock(return_value=None)
    monkeypatch.setattr("ucode.smart_routing.codex_routing.route_pre_tool_use", route)
    hook_args = [
        "codex-router-hook",
        "route-subagent",
        "--host",
        "https://example.com",
        "--model",
        "system.ai.gpt-5-6-sol",
    ]
    payload = '{"tool_name":"collaboration.spawn_agent","tool_input":{"message":"fix it"}}'

    assert runner.invoke(cli.app, ["codex", "--disable-smart-routing"], env=env).exit_code == 0
    assert runner.invoke(cli.app, hook_args, input=payload, env=env).exit_code == 0
    route.assert_not_called()

    assert runner.invoke(cli.app, ["codex", "--enable-smart-routing"], env=env).exit_code == 0
    assert runner.invoke(cli.app, hook_args, input=payload, env=env).exit_code == 0
    route.assert_called_once()
