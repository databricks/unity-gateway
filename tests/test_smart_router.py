"""Tests for launch-scoped Smart Router controls and skill installation."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from ucode import cli
from ucode.smart_routing import bundled_skill, session, v2

runner = CliRunner()


def _session_env(tmp_path: Path, enabled: bool = True) -> dict[str, str]:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"version": 1, "enabled": enabled}), encoding="utf-8")
    return {session.SESSION_STATE_ENV_VAR: str(path)}


class TestSmartRouterCommand:
    def test_hidden_from_top_level_help(self):
        result = runner.invoke(cli.app, ["--help"])

        assert result.exit_code == 0
        assert "smart-router" not in result.output

    def test_fails_outside_smart_routed_session(self, monkeypatch):
        monkeypatch.delenv(session.SESSION_STATE_ENV_VAR, raising=False)

        result = runner.invoke(cli.app, ["smart-router", "on"])

        assert result.exit_code == 1
        assert "only inside a smart-routed Claude or" in result.output
        assert "Codex session" in result.output
        assert "--enable-smart-routing" in result.output

    def test_bare_command_reports_usage_without_session_state(self, monkeypatch):
        monkeypatch.delenv(session.SESSION_STATE_ENV_VAR, raising=False)

        result = runner.invoke(cli.app, ["smart-router"])

        assert result.exit_code == 0
        assert "Smart Router: on" not in result.output
        assert "ug smart-router [on|off]" in result.output

    def test_on_and_off_are_idempotent(self, tmp_path, monkeypatch):
        env = _session_env(tmp_path)
        state_path = Path(env[session.SESSION_STATE_ENV_VAR])
        monkeypatch.setenv(session.SESSION_STATE_ENV_VAR, str(state_path))

        for action, expected in (("off", False), ("off", False), ("on", True), ("on", True)):
            result = runner.invoke(cli.app, ["smart-router", action])
            assert result.exit_code == 0
            assert json.loads(state_path.read_text())["enabled"] is expected

    def test_rejects_unknown_action(self, tmp_path, monkeypatch):
        env = _session_env(tmp_path)
        monkeypatch.setenv(session.SESSION_STATE_ENV_VAR, env[session.SESSION_STATE_ENV_VAR])

        result = runner.invoke(cli.app, ["smart-router", "status"])

        assert result.exit_code == 2
        assert "Expected `on` or `off`" in result.output


class TestSessionState:
    def test_update_atomically_replaces_state(self, tmp_path):
        env = _session_env(tmp_path)
        path = Path(env[session.SESSION_STATE_ENV_VAR])
        prior_inode = path.stat().st_ino

        session.set_routing_enabled(False, env)

        assert path.stat().st_ino != prior_inode
        assert json.loads(path.read_text()) == {"version": 1, "enabled": False}
        assert list(tmp_path.glob(".*.tmp")) == []

    @pytest.mark.parametrize("contents", [None, "not json", '{"version": 9}'])
    def test_missing_or_unreadable_state_defaults_enabled(self, tmp_path, capsys, contents):
        path = tmp_path / "state.json"
        if contents is not None:
            path.write_text(contents, encoding="utf-8")
        env = {session.SESSION_STATE_ENV_VAR: str(path)}

        assert session.routing_enabled(env) is True
        assert "routing remains enabled" in capsys.readouterr().err

    def test_each_launch_starts_a_fresh_enabled_session(self, tmp_path, monkeypatch):
        monkeypatch.setattr(v2, "install_bundled_skill", lambda: None)
        # Supply unique directories while keeping the test deterministic.
        calls = iter((tmp_path / "claude", tmp_path / "codex"))
        monkeypatch.setattr(v2, "start_session", lambda: session.start_session())
        monkeypatch.setattr(session.tempfile, "mkdtemp", lambda prefix: str(next(calls)))

        with pytest.raises(RuntimeError, match="configured workspace"):
            v2.launch_claude(
                {},
                [],
                binary="claude",
                user_settings_path=tmp_path / "settings.json",
                launch_model=None,
                compose_settings=lambda _args: ({}, []),
                launch_model_args=lambda args, _model: args,
                model_name=lambda model: model,
            )
        claude_path = Path(session.session_state_path())
        session.set_routing_enabled(False)

        with pytest.raises(RuntimeError, match="configured workspace"):
            v2.launch_codex(
                {},
                [],
                binary="codex",
                start_model="gpt",
                render_overlay=lambda *_args, **_kwargs: {},
            )
        codex_path = Path(session.session_state_path())

        assert claude_path != codex_path
        assert session.routing_enabled() is True
        assert json.loads(claude_path.read_text())["enabled"] is False


class TestRoutingHookGate:
    @pytest.mark.parametrize(
        ("command", "router_target"),
        [
            ("codex-router-hook", "ucode.smart_routing.codex_routing.route_pre_tool_use"),
            ("claude-router-hook", "ucode.cli.smart_routing_v2.route_claude_pre_tool_use"),
        ],
    )
    def test_routing_skips_while_off_and_resumes_after_on(
        self, tmp_path, monkeypatch, command, router_target
    ):
        env = _session_env(tmp_path, enabled=False)
        monkeypatch.setenv(session.SESSION_STATE_ENV_VAR, env[session.SESSION_STATE_ENV_VAR])
        monkeypatch.setenv(v2.ENABLE_SUBAGENT_ROUTING_ENV_VAR, "1")
        monkeypatch.delenv("DATABRICKS_BEARER", raising=False)
        monkeypatch.delenv("OAUTH_TOKEN", raising=False)
        token_provider = Mock(return_value="token")
        monkeypatch.setattr(cli, "get_databricks_token", token_provider)
        router = Mock(return_value=None)
        monkeypatch.setattr(router_target, router)
        args = [command, "route-subagent", "--host", "https://example.com", "--model", "m"]
        payload = '{"tool_name":"Agent","tool_input":{"prompt":"task"}}'

        off = runner.invoke(cli.app, args, input=payload)
        assert off.exit_code == 0
        router.assert_not_called()
        token_provider.assert_not_called()

        session.set_routing_enabled(True)
        on = runner.invoke(cli.app, args, input=payload)
        assert on.exit_code == 0
        router.assert_called_once()
        token_provider.assert_called_once()


class TestBundledSkill:
    def test_skill_exposes_only_on_and_off_with_launching_ug(self, monkeypatch):
        monkeypatch.setattr(bundled_skill, "ug_binary", lambda: "/checkout/.venv/bin/ug")

        content = bundled_skill._skill_content()

        assert "Bash(/checkout/.venv/bin/ug smart-router on)" in content
        assert "Bash(/checkout/.venv/bin/ug smart-router off)" in content
        assert "/checkout/.venv/bin/ug smart-router on" in content
        assert "\nug smart-router" not in content
        assert "status" not in content

    def test_skill_quotes_launching_ug_path(self, monkeypatch):
        monkeypatch.setattr(
            bundled_skill, "ug_binary", lambda: "/checkout with spaces/.venv/bin/ug"
        )

        content = bundled_skill._skill_content()

        assert "'/checkout with spaces/.venv/bin/ug' smart-router off" in content

    def test_installs_both_copies_and_upgrades_unchanged_ones(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bundled_skill, "_skill_content", lambda: "version one\n")

        installed = bundled_skill.install_bundled_skill(tmp_path)

        expected = {
            tmp_path / ".claude/skills/smart-router",
            tmp_path / ".agents/skills/smart-router",
        }
        assert set(installed) == expected
        assert all((path / "SKILL.md").read_text() == "version one\n" for path in expected)

        monkeypatch.setattr(bundled_skill, "_skill_content", lambda: "version two\n")
        bundled_skill.install_bundled_skill(tmp_path)
        assert all((path / "SKILL.md").read_text() == "version two\n" for path in expected)

    def test_preserves_unowned_name_collision(self, tmp_path, capsys):
        collision = tmp_path / ".claude/skills/smart-router"
        collision.mkdir(parents=True)
        (collision / "SKILL.md").write_text("user authored\n")

        installed = bundled_skill.install_bundled_skill(tmp_path)

        assert collision not in installed
        assert (collision / "SKILL.md").read_text() == "user authored\n"
        assert "Kept existing" in capsys.readouterr().out

    def test_revert_removes_only_unchanged_owned_copies(self, tmp_path):
        installed = bundled_skill.install_bundled_skill(tmp_path)
        claude_copy, codex_copy = installed
        (claude_copy / "SKILL.md").write_text("user edit\n")

        results = bundled_skill.revert_bundled_skill()

        assert results[claude_copy] is False
        assert results[codex_copy] is True
        assert claude_copy.exists()
        assert not codex_copy.exists()
