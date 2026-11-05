"""Auto capability, inactive provisioning and validated session transitions."""

import json

import pytest
from typer.testing import CliRunner

from ucode import cli
from ucode.constants import SMART_ROUTING_ENV_KEYS
from ucode.smart_routing import config, session_env

AUTO = config.SUBAGENT_ORCH_AUTO_PICKER_V0_CLAUDE_ONLY


@pytest.mark.parametrize("mode", ["auto", "concrete"])
def test_internal_picker_writer_and_legacy_toggle_guard(monkeypatch, tmp_path, mode):
    controls = tmp_path / "env.json"
    controls.write_text("{}")
    monkeypatch.setenv(session_env.SESSION_ENV_VAR, str(controls))
    monkeypatch.delenv("SMART_ROUTER_CONFIG_VERSION", raising=False)
    monkeypatch.setenv("UCODE_AUTO_PICKER_CONFIG", AUTO)
    monkeypatch.setenv("EXPOSE_AUTO_IN_MODEL_PICKER", "1")
    monkeypatch.delenv("ISAAC_SMART_ROUTER_FEEDBACK", raising=False)
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
        assert "/model" in result.output
        assert controls.read_text() == before


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
