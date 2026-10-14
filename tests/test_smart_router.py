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

    session_path = v2._prepare_smart_router_session(agent)
    skill = config_io.APP_DIR.parent / f".{agent}/skills/{SMART_ROUTER_SKILL}/SKILL.md"
    commands = re.findall(r"`([^`\n]*--(?:enable|disable)-smart-routing)`", skill.read_text())

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
            else {
                v2.ENABLE_SMART_ROUTING_ENV_VAR: "0",
                v2.ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
                v2.ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "0",
            }
        )
        assert json.loads(session_path.read_text()) == expected


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


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize("orchestration", ["0", "1"])
@pytest.mark.parametrize("launch_mode", [("0", "0"), ("1", "0"), ("0", "1"), (None, None)])
def test_toggle_restores_launch_mode_and_routes_only_when_on(
    monkeypatch, agent, orchestration, launch_mode
):
    for key, value in zip(
        (v2.ENABLE_SMART_ROUTING_ENV_VAR, v2.ENABLE_SUBAGENT_ROUTING_ENV_VAR),
        launch_mode,
        strict=True,
    ):
        if value is not None:
            monkeypatch.setenv(key, value)
    monkeypatch.setenv(v2.ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR, orchestration)
    path = v2._prepare_smart_router_session(agent)
    baseline = path.with_name("baseline.json").read_bytes()
    route = Mock(return_value=None)
    monkeypatch.setattr(
        f"ucode.smart_routing.{'codex_routing' if agent == 'codex' else 'v2'}."
        f"{'route_pre_tool_use' if agent == 'codex' else 'route_claude_pre_tool_use'}",
        route,
    )
    monkeypatch.setenv("DATABRICKS_BEARER", "token")
    hook_args = [
        f"{agent}-router-hook",
        "route-subagent",
        "--host",
        "https://example.com",
        "--model",
        "system.ai.gpt-5-6-sol" if agent == "codex" else "system.ai.claude-sonnet-4-6",
    ]
    payload = json.dumps({"tool_name": "Agent", "tool_input": {"prompt": "inspect the parser"}})
    expected_on = {
        v2.ENABLE_SMART_ROUTING_ENV_VAR: launch_mode[0] or "0",
        v2.ENABLE_SUBAGENT_ROUTING_ENV_VAR: (launch_mode[1] or "0" if "1" in launch_mode else "1"),
        v2.ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: orchestration,
    }
    for action in ("disable", "enable", "disable", "enable"):
        # Claude's native off command also zeros its process environment.
        for key in v2.SMART_ROUTING_ENV_KEYS:
            monkeypatch.setenv(key, "0")
        result = runner.invoke(cli.app, [agent, f"--{action}-smart-routing"])
        assert result.exit_code == 0, result.output
        expected = (
            expected_on if action == "enable" else dict.fromkeys(v2.SMART_ROUTING_ENV_KEYS, "0")
        )
        assert json.loads(path.read_text()) == expected
        assert path.with_name("baseline.json").read_bytes() == baseline
        route.reset_mock()
        result = runner.invoke(cli.app, hook_args, input=payload)
        assert result.exit_code == 0, result.output
        assert route.call_count == (action == "enable")


@pytest.mark.parametrize("baseline", ['{"UNRELATED_KEY":"1"}', "{}", "not JSON"])
def test_toggle_rejects_corrupt_launch_baseline(baseline):
    path = v2._prepare_smart_router_session("codex")
    path.with_name("baseline.json").write_text(baseline)
    before = path.read_bytes()

    result = runner.invoke(cli.app, ["codex", "--enable-smart-routing"])

    assert result.exit_code == 1
    assert "launch baseline" in result.output
    assert path.read_bytes() == before


def test_session_on_overrides_disabled_agent_preset(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_CONFIG_VERSION", "subagent_orch_v0_claude_only")
    path = v2._prepare_smart_router_session("codex")
    assert not v2.smart_routing_enabled(None, default=False, agent="codex")
    result = runner.invoke(cli.app, ["codex", "--enable-smart-routing"])
    assert result.exit_code == 0, result.output
    effective = session_env.effective_environment(agent="codex")
    assert v2.smart_routing_enabled(effective, default=False, agent="codex")
    assert effective[v2.ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR] == "0"
    assert json.loads(path.read_text())[v2.ENABLE_SUBAGENT_ROUTING_ENV_VAR] == "1"
