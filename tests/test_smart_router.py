"""Behavioral tests for session-local Smart Router controls."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from ucode import cli
from ucode.constants import SMART_ROUTING_ENV_KEYS
from ucode.smart_routing import session_env, v2

runner = CliRunner()


def _session_env(tmp_path: Path, overrides: dict[str, str] | None = None) -> dict[str, str]:
    path = tmp_path / "env.json"
    path.write_text(json.dumps(overrides or {}), encoding="utf-8")
    return {session_env.SESSION_ENV_VAR: str(path)}


class TestLauncherFlags:
    def test_standalone_command_is_not_advertised(self):
        result = runner.invoke(cli.app, ["--help"])

        assert result.exit_code == 0
        assert "smart-router" not in result.output

    @pytest.mark.parametrize("tool", ["claude", "codex"])
    def test_flags_are_mutually_exclusive(self, tool):
        result = runner.invoke(
            cli.app,
            [tool, "--enable-smart-routing", "--disable-smart-routing"],
        )

        assert result.exit_code == 2
        assert "mutually" in result.output
        assert "exclusive" in result.output

    def test_session_toggle_round_trip_is_idempotent(self, tmp_path, monkeypatch):
        inherited = _session_env(tmp_path)
        monkeypatch.setenv(session_env.SESSION_ENV_VAR, inherited[session_env.SESSION_ENV_VAR])
        monkeypatch.setenv(v2.ENABLE_SUBAGENT_ROUTING_ENV_VAR, "1")
        monkeypatch.setattr(cli, "_launch_tool", Mock(side_effect=AssertionError("launched")))

        for _ in range(2):
            result = runner.invoke(cli.app, ["codex", "--disable-smart-routing"])
            assert result.exit_code == 0
        assert not v2.smart_routing_enabled(session_env.effective_environment())

        for _ in range(2):
            result = runner.invoke(cli.app, ["codex", "--enable-smart-routing"])
            assert result.exit_code == 0
        assert json.loads(Path(inherited[session_env.SESSION_ENV_VAR]).read_text()) == {}
        assert session_env.effective_environment()[v2.ENABLE_SUBAGENT_ROUTING_ENV_VAR] == "1"

    def test_session_flag_toggles_even_with_forwarded_arguments(self, tmp_path, monkeypatch):
        inherited = _session_env(tmp_path)
        monkeypatch.setenv(session_env.SESSION_ENV_VAR, inherited[session_env.SESSION_ENV_VAR])
        monkeypatch.setattr(cli, "_launch_tool", Mock(side_effect=AssertionError("launched")))

        result = runner.invoke(
            cli.app,
            ["claude", "--disable-smart-routing", "--", "prompt"],
        )

        assert result.exit_code == 0
        state = json.loads(Path(inherited[session_env.SESSION_ENV_VAR]).read_text())
        assert state == dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")

    def test_invalid_session_pointer_does_not_launch(self, tmp_path, monkeypatch):
        monkeypatch.setenv(session_env.SESSION_ENV_VAR, str(tmp_path / "missing.json"))
        launch = Mock(side_effect=AssertionError("launched"))
        monkeypatch.setattr(cli, "_launch_tool", launch)

        result = runner.invoke(cli.app, ["claude", "--disable-smart-routing"])

        assert result.exit_code == 1
        assert "session environment" in result.output
        launch.assert_not_called()


class TestSessionEnvironment:
    def test_update_atomically_replaces_file(self, tmp_path):
        env = _session_env(tmp_path)
        path = Path(env[session_env.SESSION_ENV_VAR])
        prior_inode = path.stat().st_ino

        session_env.set_session_environment(dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0"), env)

        assert path.stat().st_ino != prior_inode
        assert json.loads(path.read_text()) == dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")

    def test_rejects_non_allowlisted_environment_keys(self, tmp_path, capsys):
        env = _session_env(tmp_path, {"OAUTH_TOKEN": "not-a-token"})
        inherited = {**env, "OAUTH_TOKEN": "inherited-token"}

        assert session_env.effective_environment(inherited) == inherited
        assert "using the inherited environment" in capsys.readouterr().err

    @pytest.mark.parametrize("contents", [None, "not json", "[]"])
    def test_bad_file_falls_back_to_inherited_environment(self, tmp_path, capsys, contents):
        path = tmp_path / "env.json"
        if contents is not None:
            path.write_text(contents, encoding="utf-8")
        inherited = {
            session_env.SESSION_ENV_VAR: str(path),
            v2.ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
        }

        effective = session_env.effective_environment(inherited)

        assert v2.smart_routing_enabled(effective)
        assert "using the inherited environment" in capsys.readouterr().err

    def test_sessions_are_isolated(self, tmp_path, monkeypatch):
        directories = iter((tmp_path / "first", tmp_path / "second"))
        monkeypatch.setattr(session_env.tempfile, "mkdtemp", lambda prefix: str(next(directories)))
        first_env = {v2.ENABLE_SMART_ROUTING_ENV_VAR: "1"}
        second_env = {v2.ENABLE_SMART_ROUTING_ENV_VAR: "1"}
        first = session_env.start_session(first_env)
        session_env.set_session_environment(
            dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0"),
            first_env,
        )
        second = session_env.start_session(second_env)

        assert first != second
        assert json.loads(first.read_text()) == dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")
        assert json.loads(second.read_text()) == {}


class TestRoutingHookGate:
    @pytest.mark.parametrize(
        ("command", "router_target"),
        [
            ("codex-router-hook", "ucode.smart_routing.codex_routing.route_pre_tool_use"),
            ("claude-router-hook", "ucode.cli.smart_routing_v2.route_claude_pre_tool_use"),
        ],
    )
    def test_off_skips_routing_and_on_resumes(self, tmp_path, monkeypatch, command, router_target):
        env = _session_env(tmp_path, dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0"))
        monkeypatch.setenv(session_env.SESSION_ENV_VAR, env[session_env.SESSION_ENV_VAR])
        monkeypatch.setenv(v2.ENABLE_SUBAGENT_ROUTING_ENV_VAR, "1")
        monkeypatch.delenv("DATABRICKS_BEARER", raising=False)
        monkeypatch.delenv("OAUTH_TOKEN", raising=False)
        token_provider = Mock(return_value="token")
        router = Mock(return_value=None)
        monkeypatch.setattr(cli, "get_databricks_token", token_provider)
        monkeypatch.setattr(router_target, router)
        args = [command, "route-subagent", "--host", "https://example.com", "--model", "m"]
        payload = '{"tool_name":"Agent","tool_input":{"prompt":"task"}}'

        assert runner.invoke(cli.app, args, input=payload).exit_code == 0
        router.assert_not_called()
        token_provider.assert_not_called()

        session_env.set_session_environment({}, env)
        assert runner.invoke(cli.app, args, input=payload).exit_code == 0
        router.assert_called_once()
        token_provider.assert_called_once()
