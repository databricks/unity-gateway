"""Tests for launch-scoped Smart Router controls and skill installation."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from ucode import cli
from ucode.constants import ENABLE_SMART_ROUTING_ENV_VAR, SMART_ROUTING_ENV_KEYS
from ucode.smart_routing import bundled_skill, session_env, v2

runner = CliRunner()


def _session_env(
    tmp_path: Path,
    *,
    overrides: dict[str, str | None] | None = None,
) -> dict[str, str]:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(overrides or {}),
        encoding="utf-8",
    )
    return {session_env.SESSION_ENV_VAR: str(path)}


class TestSmartRouterLauncherFlags:
    def test_standalone_command_is_removed(self):
        result = runner.invoke(cli.app, ["--help"])

        assert result.exit_code == 0
        assert "smart-router" not in result.output

    @pytest.mark.parametrize("tool", ["claude", "codex"])
    def test_launcher_help_exposes_both_flags(self, tool):
        result = runner.invoke(cli.app, [tool, "--help"])

        assert result.exit_code == 0
        assert "--enable-smart-routing" in result.output
        assert "--disable-smart-routing" in result.output

    @pytest.mark.parametrize("tool", ["claude", "codex"])
    def test_flags_are_mutually_exclusive(self, tool):
        result = runner.invoke(
            cli.app,
            [tool, "--enable-smart-routing", "--disable-smart-routing"],
        )

        assert result.exit_code == 2
        assert "mutually" in result.output
        assert "exclusive" in result.output

    @pytest.mark.parametrize(
        ("tool", "flag", "expected"),
        [
            ("claude", "--disable-smart-routing", False),
            ("codex", "--enable-smart-routing", True),
        ],
    )
    def test_explicit_override_toggles_current_session(
        self, tmp_path, monkeypatch, tool, flag, expected
    ):
        env = _session_env(tmp_path)
        monkeypatch.setenv(session_env.SESSION_ENV_VAR, env[session_env.SESSION_ENV_VAR])
        monkeypatch.setattr(cli, "_launch_tool", Mock(side_effect=AssertionError("launched")))

        result = runner.invoke(cli.app, [tool, flag])

        assert result.exit_code == 0
        state = json.loads(Path(env[session_env.SESSION_ENV_VAR]).read_text())
        assert state == (dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0") if not expected else {})

    @pytest.mark.parametrize("tool", ["claude", "codex"])
    def test_toggle_is_idempotent(self, tmp_path, monkeypatch, tool):
        env = _session_env(tmp_path)
        monkeypatch.setenv(session_env.SESSION_ENV_VAR, env[session_env.SESSION_ENV_VAR])
        monkeypatch.setattr(cli, "_launch_tool", Mock(side_effect=AssertionError("launched")))

        for flag, expected in (
            ("--disable-smart-routing", False),
            ("--enable-smart-routing", True),
        ):
            result = runner.invoke(cli.app, [tool, flag])
            assert result.exit_code == 0
            state = json.loads(Path(env[session_env.SESSION_ENV_VAR]).read_text())
            assert state == (dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0") if not expected else {})

    def test_enable_restores_the_inherited_routing_mode(self, tmp_path, monkeypatch):
        monkeypatch.setenv(v2.ENABLE_SUBAGENT_ROUTING_ENV_VAR, "1")
        env = _session_env(
            tmp_path,
            overrides=dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0"),
        )
        monkeypatch.setenv(session_env.SESSION_ENV_VAR, env[session_env.SESSION_ENV_VAR])
        monkeypatch.setattr(cli, "_launch_tool", Mock(side_effect=AssertionError("launched")))

        result = runner.invoke(cli.app, ["codex", "--enable-smart-routing"])

        assert result.exit_code == 0
        assert json.loads(Path(env[session_env.SESSION_ENV_VAR]).read_text()) == {}
        assert session_env.effective_environment()[v2.ENABLE_SUBAGENT_ROUTING_ENV_VAR] == "1"

    @pytest.mark.parametrize("tool", ["claude", "codex"])
    def test_override_toggles_without_inspecting_launch_request(self, tmp_path, monkeypatch, tool):
        env = _session_env(tmp_path)
        monkeypatch.setenv(session_env.SESSION_ENV_VAR, env[session_env.SESSION_ENV_VAR])
        monkeypatch.setattr(cli, "_launch_tool", Mock(side_effect=AssertionError("launched")))

        result = runner.invoke(cli.app, [tool, "--disable-smart-routing", "--", "prompt"])

        assert result.exit_code == 0
        state = json.loads(Path(env[session_env.SESSION_ENV_VAR]).read_text())
        assert state == dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")

    def test_disable_outside_session_is_an_ordinary_launch(self, monkeypatch):
        monkeypatch.delenv(session_env.SESSION_ENV_VAR, raising=False)
        launch = Mock()
        monkeypatch.setattr(cli, "_launch_tool", launch)

        result = runner.invoke(cli.app, ["codex", "--disable-smart-routing"])

        assert result.exit_code == 0
        launch.assert_called_once()
        assert launch.call_args.kwargs["smart_routing_override"] is False

    def test_invalid_session_pointer_reports_error_without_nested_launch(
        self, tmp_path, monkeypatch
    ):
        state_path = tmp_path / "missing-state.json"
        monkeypatch.setenv(session_env.SESSION_ENV_VAR, str(state_path))
        launch = Mock(side_effect=AssertionError("launched"))
        monkeypatch.setattr(cli, "_launch_tool", launch)

        result = runner.invoke(cli.app, ["claude", "--disable-smart-routing"])

        assert result.exit_code == 1
        assert "session environment" in result.output
        launch.assert_not_called()


class TestSessionEnvironment:
    def test_update_atomically_replaces_state(self, tmp_path):
        env = _session_env(tmp_path)
        path = Path(env[session_env.SESSION_ENV_VAR])
        prior_inode = path.stat().st_ino

        session_env.set_session_environment({ENABLE_SMART_ROUTING_ENV_VAR: "0"}, env)

        assert path.stat().st_ino != prior_inode
        state = json.loads(path.read_text())
        assert state == {ENABLE_SMART_ROUTING_ENV_VAR: "0"}
        assert list(tmp_path.glob(".*.tmp")) == []

    def test_overlay_rejects_non_allowlisted_environment_keys(self, tmp_path, capsys):
        path = tmp_path / "state.json"
        path.write_text(
            json.dumps({"OAUTH_TOKEN": "not-a-token"}),
            encoding="utf-8",
        )
        inherited = {
            session_env.SESSION_ENV_VAR: str(path),
            "OAUTH_TOKEN": "inherited-token",
        }

        assert session_env.effective_environment(inherited) == inherited
        assert "using the inherited environment" in capsys.readouterr().err

    @pytest.mark.parametrize("contents", [None, "not json", "[]"])
    def test_missing_or_unreadable_state_defaults_enabled(self, tmp_path, capsys, contents):
        path = tmp_path / "state.json"
        if contents is not None:
            path.write_text(contents, encoding="utf-8")
        env = {session_env.SESSION_ENV_VAR: str(path)}

        assert session_env.effective_environment(env) == env
        assert "using the inherited environment" in capsys.readouterr().err

    def test_each_launch_starts_a_fresh_enabled_session(self, tmp_path, monkeypatch):
        monkeypatch.setattr(v2, "install_bundled_skill", lambda: None)
        # Supply unique directories while keeping the test deterministic.
        calls = iter((tmp_path / "claude", tmp_path / "codex"))
        monkeypatch.setattr(v2, "start_session", lambda: session_env.start_session())
        monkeypatch.setattr(session_env.tempfile, "mkdtemp", lambda prefix: str(next(calls)))
        monkeypatch.setenv(v2.ENABLE_SMART_ROUTING_ENV_VAR, "1")

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
        claude_path = Path(session_env.session_env_path())
        session_env.set_session_environment(dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0"))

        with pytest.raises(RuntimeError, match="configured workspace"):
            v2.launch_codex(
                {},
                [],
                binary="codex",
                start_model="gpt",
                render_overlay=lambda *_args, **_kwargs: {},
            )
        codex_path = Path(session_env.session_env_path())

        assert claude_path != codex_path
        assert session_env.effective_environment()[v2.ENABLE_SMART_ROUTING_ENV_VAR] == "1"
        assert json.loads(claude_path.read_text()) == dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")


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
        env = _session_env(
            tmp_path,
            overrides=dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0"),
        )
        monkeypatch.setenv(session_env.SESSION_ENV_VAR, env[session_env.SESSION_ENV_VAR])
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

        session_env.set_session_environment(dict.fromkeys(SMART_ROUTING_ENV_KEYS))
        on = runner.invoke(cli.app, args, input=payload)
        assert on.exit_code == 0
        router.assert_called_once()
        token_provider.assert_called_once()


class TestBundledSkill:
    @pytest.mark.parametrize(
        "launcher",
        ["claude", "codex"],
    )
    def test_skill_exposes_only_on_and_off_with_launching_ug(self, monkeypatch, launcher):
        monkeypatch.setattr(bundled_skill, "ug_binary", lambda: "/checkout/.venv/bin/ug")

        content = bundled_skill._skill_content(launcher)

        assert f"Bash(/checkout/.venv/bin/ug {launcher} --enable-smart-routing)" in content
        assert f"Bash(/checkout/.venv/bin/ug {launcher} --disable-smart-routing)" in content
        assert f"/checkout/.venv/bin/ug {launcher} --enable-smart-routing" in content
        assert "\nug smart-router" not in content
        assert "status" not in content

    def test_skill_quotes_launching_ug_path(self, monkeypatch):
        monkeypatch.setattr(
            bundled_skill, "ug_binary", lambda: "/checkout with spaces/.venv/bin/ug"
        )

        content = bundled_skill._skill_content("codex")

        assert "'/checkout with spaces/.venv/bin/ug' codex --disable-smart-routing" in content

    def test_installs_harness_specific_copies(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bundled_skill, "ug_binary", lambda: "/bin/ug")

        installed = bundled_skill.install_bundled_skill(tmp_path)

        assert len(installed) == 2
        claude_content = (tmp_path / ".claude/skills/smart-router/SKILL.md").read_text()
        codex_content = (tmp_path / ".agents/skills/smart-router/SKILL.md").read_text()
        assert "/bin/ug claude --enable-smart-routing" in claude_content
        assert "/bin/ug codex --enable-smart-routing" in codex_content
        assert " /bin/ug codex --enable-smart-routing" not in claude_content
        assert " /bin/ug claude --enable-smart-routing" not in codex_content

    def test_installs_both_copies_and_upgrades_unchanged_ones(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bundled_skill, "_skill_content", lambda _launcher: "version one\n")

        installed = bundled_skill.install_bundled_skill(tmp_path)

        expected = {
            tmp_path / ".claude/skills/smart-router",
            tmp_path / ".agents/skills/smart-router",
        }
        assert set(installed) == expected
        assert all((path / "SKILL.md").read_text() == "version one\n" for path in expected)

        monkeypatch.setattr(bundled_skill, "_skill_content", lambda _launcher: "version two\n")
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
