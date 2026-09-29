"""Behavioral coverage for session-local Smart Router controls."""

import os
from pathlib import Path
from unittest.mock import Mock

from typer.testing import CliRunner

from ucode import cli, config_io
from ucode.packaged_skills import SMART_ROUTER_SKILL
from ucode.smart_routing import session_env, v2

runner = CliRunner()


def test_smart_routed_session_installs_skill(tmp_path, monkeypatch):
    monkeypatch.delenv(session_env.SESSION_ENV_VAR, raising=False)

    v2._prepare_smart_router_session()

    home = config_io.APP_DIR.parent
    assert home.joinpath(f".claude/skills/{SMART_ROUTER_SKILL}/SKILL.md").is_file()
    assert home.joinpath(f".agents/skills/{SMART_ROUTER_SKILL}/SKILL.md").is_file()
    assert Path(os.environ[session_env.SESSION_ENV_VAR]).is_file()


def test_skill_command_controls_routing_hook(tmp_path, monkeypatch):
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

    assert runner.invoke(cli.app, ["smart-router", "off"], env=env).exit_code == 0
    assert runner.invoke(cli.app, hook_args, input=payload, env=env).exit_code == 0
    route.assert_not_called()

    assert runner.invoke(cli.app, ["smart-router", "on"], env=env).exit_code == 0
    assert runner.invoke(cli.app, hook_args, input=payload, env=env).exit_code == 0
    route.assert_called_once()
