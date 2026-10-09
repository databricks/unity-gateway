"""Desktop hooks on successful configure paths, not ordinary agent launches."""

from unittest.mock import Mock

import pytest

from ucode import cli, desktop_setup


@pytest.fixture
def configuration(monkeypatch):
    state = {
        "workspace": "https://workspace.example",
        "profile": "chosen-profile",
        "claude_models": {"haiku": "system.ai.claude-haiku-4-5"},
        "available_tools": ["claude", "codex"],
        "last_configured_tools": ["claude", "codex"],
    }
    monkeypatch.setattr(cli, "_configure_shared_workspace_states", lambda *a, **k: [state])
    monkeypatch.setattr(cli, "save_state", lambda *a, **k: None)
    monkeypatch.setattr(cli, "install_tool_binary", lambda *a, **k: True)
    monkeypatch.setattr(cli, "install_databricks_ai_tools_for_agents", lambda *a, **k: None)
    monkeypatch.setattr(cli, "check_gateway_endpoint", lambda *a, **k: True)
    monkeypatch.setattr(cli, "_configure_managed_mcp_servers", lambda *a, **k: [])
    monkeypatch.setattr(cli, "_configure_managed_skills", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_summarize_managed_config", lambda *a, **k: None)
    monkeypatch.setattr(cli, "refresh_managed_config", lambda *a, **k: (None, False))
    monkeypatch.setattr(cli, "configure_single_tool", lambda *a, **k: state)
    monkeypatch.setattr(cli, "configure_selected_tools", lambda *a, **k: state)
    return state


@pytest.mark.parametrize("flow", ["single", "managed", "selected"])
def test_successful_claude_setup_configures_desktop_once(configuration, monkeypatch, flow):
    configure = Mock()
    monkeypatch.setattr(desktop_setup, "configure_desktop_after_claude", configure)
    args = {"selected_tools": ["claude"]}
    if flow == "single":
        args = {"tool": "claude"}
    if flow == "managed":
        args = {}
        monkeypatch.setattr(
            cli,
            "refresh_managed_config",
            lambda *a, **k: (
                {
                    "enabled_agents": {
                        "claude": {"model_config": {"unity_catalog_location": "main.models"}}
                    }
                },
                False,
            ),
        )
    assert (
        cli._configure_workspace_command(
            workspaces=[("https://workspace.example", "chosen-profile")], **args
        )
        == 0
    )
    configure.assert_called_once_with(
        configuration,
        **(
            {"parent_schema": "main.models"}
            if flow == "managed"
            else {"parent_schema": None}
            if flow == "single"
            else {}
        ),
    )


def test_failed_claude_is_not_setup_from_prior_available_tools(configuration, monkeypatch):
    configuration["last_configured_tools"] = ["codex"]
    configure = Mock()
    monkeypatch.setattr(desktop_setup, "configure_desktop_after_claude", configure)
    assert (
        cli._configure_workspace_command(
            selected_tools=["claude", "codex"],
            workspaces=[("https://workspace.example", "chosen-profile")],
        )
        == 0
    )
    configure.assert_not_called()


def test_codex_only_does_not_configure_desktop(configuration, monkeypatch):
    configure = Mock()
    monkeypatch.setattr(desktop_setup, "configure_desktop_after_claude", configure)
    assert (
        cli._configure_workspace_command(
            tool="codex", workspaces=[("https://workspace.example", "chosen-profile")]
        )
        == 0
    )
    configure.assert_not_called()


@pytest.mark.parametrize("flow", ["single", "managed", "selected"])
def test_desktop_failure_preserves_successful_configure_exit(
    configuration, monkeypatch, capsys, flow
):
    def fail(*a, **k):
        raise OSError("Desktop profile is not writable")

    monkeypatch.setattr(desktop_setup, "_configure_desktop", fail)
    monkeypatch.setattr(desktop_setup.sys, "platform", "darwin")
    args = {"selected_tools": ["claude"]}
    if flow == "single":
        args = {"tool": "claude"}
    if flow == "managed":
        args = {}
        monkeypatch.setattr(
            cli,
            "refresh_managed_config",
            lambda *a, **k: ({"enabled_agents": {"claude": {}}}, False),
        )
    assert (
        cli._configure_workspace_command(
            workspaces=[("https://workspace.example", "chosen-profile")], **args
        )
        == 0
    )
    assert "Desktop profile is not writable" in capsys.readouterr().out


def test_desktop_revert_failure_retains_state_for_retry(configuration, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_state", lambda: configuration)
    monkeypatch.setattr(cli, "revert_mcp_configs", lambda *a: {})
    monkeypatch.setattr(cli.claude_agent, "revert_managed_settings", lambda: "unchanged")
    monkeypatch.setattr(cli.codex_agent, "revert_managed_config", lambda: "unchanged")
    monkeypatch.setattr(cli, "restore_file", lambda *a: False)
    monkeypatch.setattr(cli, "revert_legacy_shared_config", lambda: False)
    monkeypatch.setattr(desktop_setup, "revert_desktop", lambda *a: "failed")
    clear = Mock()
    monkeypatch.setattr(cli, "clear_state", clear)
    assert cli.revert() == 1
    clear.assert_not_called()
    assert "ug state retained" in capsys.readouterr().out
