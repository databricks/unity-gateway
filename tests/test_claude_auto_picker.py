"""Auto capability, inactive provisioning and validated session transitions."""

import json
from unittest.mock import Mock, patch

import pytest
from typer.testing import CliRunner

from ucode import cli
from ucode.constants import SMART_ROUTING_ENV_KEYS
from ucode.smart_routing import config, session_env, v2

AUTO = config.SUBAGENT_ORCH_AUTO_PICKER_V0_CLAUDE_ONLY


def test_auto_capability_alone_starts_off_with_assets_available():
    env = {
        "UCODE_AUTO_PICKER_CONFIG": AUTO,
        "EXPOSE_AUTO_IN_MODEL_PICKER": "1",
        **dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0"),
    }
    assert all(env[key] == "0" for key in SMART_ROUTING_ENV_KEYS)
    assert not v2.smart_routing_enabled(env, default=False, agent="claude")
    options = v2._claude_mod_launch_options(env)
    assert options["EXPOSE_AUTO_IN_MODEL_PICKER"] == "1"
    assert options["orchestratorInstructions"]


def test_auto_launch_only_removes_claude_command_skill(monkeypatch, tmp_path):
    monkeypatch.delenv("SMART_ROUTER_CONFIG_VERSION", raising=False)
    monkeypatch.setenv("UCODE_AUTO_PICKER_CONFIG", AUTO)
    monkeypatch.setenv("EXPOSE_AUTO_IN_MODEL_PICKER", "1")
    with (
        patch.object(v2, "install_skill") as install,
        patch.object(v2, "uninstall_agent_skill") as remove,
        patch.object(v2, "start_session", return_value=tmp_path),
    ):
        v2._prepare_smart_router_session("claude")
    remove.assert_called_once_with(v2.SMART_ROUTER_SKILL, "claude", v2.config_io.APP_DIR.parent)
    assert [call.args[0] for call in install.call_args_list] == [v2.SMART_ROUTER_ORCHESTRATOR_SKILL]


@pytest.mark.parametrize("mode", ["auto", "concrete"])
def test_internal_picker_writer_and_legacy_toggle_guard(monkeypatch, tmp_path, mode):
    controls = tmp_path / "env.json"
    controls.write_text("{}")
    monkeypatch.setenv(session_env.SESSION_ENV_VAR, str(controls))
    monkeypatch.delenv("SMART_ROUTER_CONFIG_VERSION", raising=False)
    monkeypatch.setenv("UCODE_AUTO_PICKER_CONFIG", AUTO)
    monkeypatch.setenv("EXPOSE_AUTO_IN_MODEL_PICKER", "1")
    monkeypatch.delenv("ISAAC_SMART_ROUTER_FEEDBACK", raising=False)
    launch = Mock(side_effect=AssertionError("Session controls must not launch an agent"))
    monkeypatch.setattr(cli, "launch_agent", launch)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["claude-auto-mode", mode])
    assert result.exit_code == 0, result.output
    expected = dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")
    if mode == "auto":
        expected.update(
            ENABLE_SMART_ROUTING_SUBAGENT_ONLY="1", ENABLE_SMART_ROUTER_ORCHESTRATOR="1"
        )
    assert json.loads(controls.read_text()) == expected
    before = controls.read_text()
    for flag in ("--enable-smart-routing", "--disable-smart-routing"):
        result = runner.invoke(cli.app, ["claude", flag])
        assert result.exit_code == 2, result.output
        assert "/model" in result.output
        assert controls.read_text() == before
    for flag in ("--enable-smart-routing", "--disable-smart-routing"):
        result = runner.invoke(cli.app, ["codex", flag])
        assert result.exit_code == 0, result.output
        assert "/model" not in result.output
        assert json.loads(controls.read_text()) == (
            {} if flag == "--enable-smart-routing" else dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")
        )
    launch.assert_not_called()


def test_internal_writer_requires_capability_and_feedback(monkeypatch, tmp_path):
    controls = tmp_path / "env.json"
    controls.write_text("{}")
    monkeypatch.setenv(session_env.SESSION_ENV_VAR, str(controls))
    monkeypatch.delenv("SMART_ROUTER_CONFIG_VERSION", raising=False)
    monkeypatch.delenv("UCODE_AUTO_PICKER_CONFIG", raising=False)
    monkeypatch.delenv("EXPOSE_AUTO_IN_MODEL_PICKER", raising=False)
    runner = CliRunner()
    assert runner.invoke(cli.app, ["claude-auto-mode", "auto"]).exit_code != 0
    monkeypatch.delenv("SMART_ROUTER_CONFIG_VERSION", raising=False)
    monkeypatch.setenv("UCODE_AUTO_PICKER_CONFIG", AUTO)
    monkeypatch.setenv("EXPOSE_AUTO_IN_MODEL_PICKER", "1")
    monkeypatch.setenv("ISAAC_SMART_ROUTER_FEEDBACK", "1")
    assert runner.invoke(cli.app, ["claude-auto-mode", "concrete"]).exit_code != 0
    assert controls.read_text() == "{}"


@pytest.mark.parametrize(
    "feedback,accepted", [("  " + "x" * 15 + "  ", False), ("  " + "x" * 16 + "  ", True)]
)
def test_required_feedback_uses_trimmed_boundary(monkeypatch, tmp_path, feedback, accepted):
    controls = tmp_path / "env.json"
    controls.write_text("{}")
    monkeypatch.setenv(session_env.SESSION_ENV_VAR, str(controls))
    monkeypatch.delenv("SMART_ROUTER_CONFIG_VERSION", raising=False)
    monkeypatch.setenv("UCODE_AUTO_PICKER_CONFIG", AUTO)
    monkeypatch.setenv("EXPOSE_AUTO_IN_MODEL_PICKER", "1")
    monkeypatch.setenv("ISAAC_SMART_ROUTER_FEEDBACK", "1")
    result = CliRunner().invoke(
        cli.app, ["claude-auto-mode", "concrete", "--smart-router-feedback", feedback]
    )
    assert result.exit_code == (0 if accepted else 2), result.output
    if accepted:
        assert json.loads(controls.read_text()) == dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")
        assert "/isaac-feedback --category smart-routing" in " ".join(result.output.split())
    else:
        assert controls.read_text() == "{}"
