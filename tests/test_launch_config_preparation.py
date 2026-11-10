"""Component coverage of CLI dispatch through the real token-backed agent adapters."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from ucode import cli, config_io
from ucode import state as state_module
from ucode.agents import gemini, kilo, opencode, pi
from ucode.agents.args import LaunchOptions

WORKSPACE = "https://example.databricks.com"
MODULES = {"gemini": gemini, "opencode": opencode, "kilo": kilo, "pi": pi}


@pytest.fixture
def launch_setup(tmp_path, monkeypatch):
    """Keep real config/state writes and dispatch; isolate auth, preflight, and native execution."""
    state = {
        "workspace": WORKSPACE,
        "available_tools": list(MODULES),
        "claude_models": {"sonnet": "claude-sonnet"},
        "gemini_models": ["gemini-default", "gemini-selected"],
        "opencode_models": {"anthropic": ["claude-sonnet"], "gemini": ["gemini-selected"]},
        "managed_configs": {},
    }
    for name in ("ensure_bootstrap_dependencies", "refresh_downloaded_skills_on_launch"):
        monkeypatch.setattr(cli, name, Mock())
    monkeypatch.setattr(cli, "load_state", lambda: state)
    monkeypatch.setattr(cli, "ensure_provider_state", lambda _tool: state)
    monkeypatch.setattr(cli, "configure_shared_state", lambda *args, **kwargs: state)
    monkeypatch.setattr(cli, "_fetch_managed_config", lambda _state: (None, False))
    monkeypatch.setattr(cli, "_fetch_budget_recommendation", lambda *args: None)
    for module in MODULES.values():
        for name, value in vars(module).copy().items():
            if name.endswith(("_PATH", "_DIR", "_HOME")) and hasattr(value, "is_absolute"):
                monkeypatch.setattr(
                    module, name, tmp_path / module.__name__.rsplit(".", 1)[-1] / name
                )
        monkeypatch.setattr(module, "agent_version", lambda *args: "1.0.0")
        monkeypatch.setattr(module, "ug_version", lambda: "0.0.0")
    proc = Mock()
    proc.wait.return_value = 0
    spawn = Mock(return_value=proc)
    monkeypatch.setattr(gemini.subprocess_cross_os, "popen", spawn)
    return SimpleNamespace(state=state, spawn=spawn)


@pytest.mark.parametrize("tool", MODULES)
def test_cli_prepares_each_agent_config_once(launch_setup, monkeypatch, tool):
    """Real writers persist the resolved model once and fetch credentials after skill refresh."""
    module = MODULES[tool]
    selected = "gemini-selected"
    saved_default = "gemini-default" if tool == "gemini" else "claude-sonnet"
    launch_setup.state[f"{tool}_default_model"] = saved_default
    # A resolved recommendation may differ from the adapter's own saved default.
    monkeypatch.setattr(cli, "resolve_launch_model", lambda *args: (launch_setup.state, selected))
    writer = Mock(wraps=module.write_tool_config)
    monkeypatch.setattr(module, "write_tool_config", writer)

    def fresh_token(*args, **kwargs):
        cli.refresh_downloaded_skills_on_launch.assert_called_once()
        return "synthetic-current-bearer"

    token = Mock(side_effect=fresh_token)
    monkeypatch.setattr(module, "get_databricks_token", token)
    saved = Mock(wraps=module.save_state)
    monkeypatch.setattr(module, "save_state", saved)

    result = CliRunner().invoke(cli.app, [tool])

    assert result.exit_code == 0, result.output
    writer.assert_called_once()
    saved.assert_called_once()
    token.assert_called_once()
    cli.refresh_downloaded_skills_on_launch.assert_called_once()
    assert launch_setup.spawn.call_args.kwargs["env"]["OAUTH_TOKEN"] == "synthetic-current-bearer"
    persisted = json.loads(state_module.STATE_PATH.read_text())
    block = persisted["workspaces"][WORKSPACE]
    assert block["last_tool"] == tool
    assert block[f"{tool}_default_model"] == saved_default
    assert "synthetic-current-bearer" not in state_module.STATE_PATH.read_text()
    if tool == "gemini":
        assert config_io.parse_dotenv(gemini.GEMINI_ENV_PATH)["GEMINI_MODEL"] == selected
    else:
        path = getattr(module, f"{tool.upper()}_CONFIG_PATH")
        assert json.loads(path.read_text())["model"].endswith("/" + selected)


@pytest.mark.parametrize("tool", ["opencode", "kilo"])
def test_forwarded_model_wins_without_writing_the_default_first(launch_setup, monkeypatch, tool):
    module = MODULES[tool]
    writer = Mock(wraps=module.write_tool_config)
    monkeypatch.setattr(module, "write_tool_config", writer)
    monkeypatch.setattr(module, "get_databricks_token", lambda *args, **kwargs: "synthetic-bearer")

    result = CliRunner().invoke(
        cli.app, [tool, "--", "run", "--model", "databricks-google/gemini-selected"]
    )

    assert result.exit_code == 0, result.output
    writer.assert_called_once()
    assert writer.call_args.args[1] == "databricks-google/gemini-selected"
    path = getattr(module, f"{tool.upper()}_CONFIG_PATH")
    assert json.loads(path.read_text())["model"] == "databricks-google/gemini-selected"
    assert launch_setup.spawn.call_args.args[0] == [
        tool,
        "run",
        "--model",
        "databricks-google/gemini-selected",
    ]


def test_gemini_provider_target_and_periodic_refresh_keep_the_selected_source(
    launch_setup, monkeypatch
):
    launch_setup.state["provider_services"] = {"gemini": "cat.schema.previous"}
    monkeypatch.setattr(
        cli, "resolve_gemini_provider_model", lambda *args, **kwargs: ("gemini-selected", None)
    )
    token = Mock(return_value="synthetic-bearer")
    monkeypatch.setattr(gemini, "get_databricks_token", token)
    writer = Mock(wraps=gemini.write_tool_config)
    monkeypatch.setattr(gemini, "write_tool_config", writer)

    result = CliRunner().invoke(cli.app, ["gemini", "--provider", "cat.schema.selected"])

    assert result.exit_code == 0, result.output
    writer.assert_called_once()
    env = launch_setup.spawn.call_args.kwargs["env"]
    assert env["GEMINI_MODEL"] == "gemini-selected"
    assert (
        "Databricks-Model-Provider-Service:cat.schema.selected" in env["GEMINI_CLI_CUSTOM_HEADERS"]
    )
    persisted = json.loads(state_module.STATE_PATH.read_text())["workspaces"][WORKSPACE]
    assert persisted["provider_services"]["gemini"] == "cat.schema.selected"

    gemini._refresh_token_once(launch_setup.state, force_refresh=True)

    assert token.call_args.kwargs == {"force_refresh": True}
    assert writer.call_count == 2
    assert writer.call_args.kwargs["provider"] == "cat.schema.selected"
    assert writer.call_args.args[1] == "gemini-selected"


@pytest.mark.parametrize("tool", ["gemini", "pi"])
def test_standalone_adapter_still_prepares_its_default(launch_setup, monkeypatch, tool):
    module = MODULES[tool]
    monkeypatch.setattr(module, "get_databricks_token", lambda *args, **kwargs: "synthetic-bearer")
    writer = Mock(wraps=module.write_tool_config)
    monkeypatch.setattr(module, "write_tool_config", writer)

    with pytest.raises(SystemExit) as exc:
        module.launch(launch_setup.state, [], options=LaunchOptions())

    assert exc.value.code == 0
    writer.assert_called_once()
    assert writer.call_args.args[1] == ("gemini-default" if tool == "gemini" else "claude-sonnet")


@pytest.mark.parametrize("tool", MODULES)
def test_managed_models_reach_config_without_replacing_the_saved_defaults(
    launch_setup, monkeypatch, tool
):
    module = MODULES[tool]
    managed = {
        "enabled_agents": {
            tool: {
                "model_config": {"default_model": "gemini-selected", "models": ["gemini-selected"]}
            }
        }
    }
    monkeypatch.setattr(cli, "_fetch_managed_config", lambda _state: (managed, False))
    monkeypatch.setattr(module, "get_databricks_token", lambda *args, **kwargs: "synthetic-bearer")
    writer = Mock(wraps=module.write_tool_config)
    monkeypatch.setattr(module, "write_tool_config", writer)

    result = CliRunner().invoke(cli.app, [tool])

    assert result.exit_code == 0, result.output
    writer.assert_called_once()
    assert writer.call_args.args[1] == "gemini-selected"
    persisted = json.loads(state_module.STATE_PATH.read_text())["workspaces"][WORKSPACE]
    assert f"{tool}_default_model" not in persisted
    assert persisted["gemini_models"] == ["gemini-default", "gemini-selected"]
    assert persisted["opencode_models"] == launch_setup.state["opencode_models"]


@pytest.mark.parametrize("tool", MODULES)
def test_dry_run_prepares_once_without_config_or_state_writes(launch_setup, monkeypatch, tool):
    module = MODULES[tool]
    monkeypatch.setattr(module, "get_databricks_token", lambda *args, **kwargs: "synthetic-bearer")
    writer = Mock(wraps=module.write_tool_config)
    monkeypatch.setattr(module, "write_tool_config", writer)
    config_io.set_dry_run(True)
    try:
        result = CliRunner().invoke(cli.app, [tool])
    finally:
        config_io.set_dry_run(False)

    assert result.exit_code == 0, result.output
    writer.assert_called_once()
    assert not state_module.STATE_PATH.exists()
    path = (
        gemini.GEMINI_ENV_PATH
        if tool == "gemini"
        else getattr(module, f"{tool.upper()}_CONFIG_PATH")
    )
    assert not path.exists()
