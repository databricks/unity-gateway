"""Desktop hooks on successful configure paths, not ordinary agent launches."""

from unittest.mock import Mock

import pytest

from ucode import cli
from ucode.agents.desktop import setup as desktop_setup


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
    monkeypatch.setattr(cli, "_configure_managed_skills", lambda *a, **k: True)
    monkeypatch.setattr(cli, "_summarize_managed_config", lambda *a, **k: None)
    monkeypatch.setattr(cli, "refresh_managed_config", lambda *a, **k: (None, False))
    monkeypatch.setattr(cli, "configure_single_tool", lambda *a, **k: state)
    monkeypatch.setattr(cli, "configure_selected_tools", lambda *a, **k: state)
    monkeypatch.setattr(desktop_setup, "configure_claude_cowork", lambda *a, **k: None)
    monkeypatch.setattr(desktop_setup, "revert_claude_cowork", lambda *a, **k: "unchanged")
    return state


@pytest.mark.parametrize("flow", ["single", "managed", "selected"])
def test_successful_claude_setup_configures_desktop_once(configuration, monkeypatch, flow):
    configure = Mock()
    monkeypatch.setattr(desktop_setup, "configure_claude_desktop", configure)
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
    revert_cowork = Mock(return_value="removed")
    monkeypatch.setattr(desktop_setup, "configure_claude_desktop", configure)
    monkeypatch.setattr(desktop_setup, "revert_claude_cowork", revert_cowork)
    assert (
        cli._configure_workspace_command(
            selected_tools=["claude", "codex"],
            workspaces=[("https://workspace.example", "chosen-profile")],
        )
        == 0
    )
    configure.assert_not_called()
    revert_cowork.assert_not_called()


def test_codex_only_does_not_configure_desktop(configuration, monkeypatch):
    configure = Mock()
    revert_cowork = Mock(return_value="removed")
    monkeypatch.setattr(desktop_setup, "configure_claude_desktop", configure)
    monkeypatch.setattr(desktop_setup, "revert_claude_cowork", revert_cowork)
    assert (
        cli._configure_workspace_command(
            tool="codex", workspaces=[("https://workspace.example", "chosen-profile")]
        )
        == 0
    )
    configure.assert_not_called()
    revert_cowork.assert_not_called()


def test_self_managed_agent_add_does_not_configure_desktop(configuration, monkeypatch):
    configure = Mock()
    monkeypatch.setattr(desktop_setup, "configure_claude_desktop", configure)
    cli._setup_single_agent(
        [("https://workspace.example", "chosen-profile")], "claude", apply_managed=False
    )
    configure.assert_not_called()


def test_single_managed_setup_passes_catalog_scope_to_desktop(configuration, monkeypatch):
    configure = Mock()
    monkeypatch.setattr(desktop_setup, "configure_claude_desktop", configure)
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
            tool="claude", workspaces=[("https://workspace.example", "chosen-profile")]
        )
        == 0
    )
    configure.assert_called_once_with(configuration, parent_schema="main.models")


def test_single_managed_claude_configures_cowork(configuration, monkeypatch):
    managed = {"enabled_agents": {"claude": {}}}
    servers = [{"name": "github", "clients": ["claude"]}]
    configure_cowork = Mock()
    configure_mcps = Mock(return_value=servers)
    monkeypatch.setattr(desktop_setup, "configure_claude_desktop", lambda *a, **k: None)
    monkeypatch.setattr(desktop_setup, "configure_claude_cowork", configure_cowork)
    monkeypatch.setattr(cli, "_configure_managed_mcp_servers", configure_mcps)
    monkeypatch.setattr(cli, "_configure_managed_skills", lambda configured: True)
    monkeypatch.setattr(cli, "refresh_managed_config", lambda *a, **k: (managed, False))

    assert (
        cli._configure_workspace_command(
            tool="claude", workspaces=[("https://workspace.example", "chosen-profile")]
        )
        == 0
    )
    configure_mcps.assert_called_once_with(managed)
    configure_cowork.assert_called_once_with(configuration, servers)


def test_single_unmanaged_claude_reconciles_empty_cowork(configuration, monkeypatch):
    configure_cowork = Mock()
    monkeypatch.setattr(desktop_setup, "configure_claude_desktop", lambda *a, **k: None)
    monkeypatch.setattr(desktop_setup, "configure_claude_cowork", configure_cowork)

    assert (
        cli._configure_workspace_command(
            tool="claude", workspaces=[("https://workspace.example", "chosen-profile")]
        )
        == 0
    )
    configure_cowork.assert_called_once_with(configuration, [])


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


def test_managed_claude_configures_cowork_after_mcp_and_skill_sync(configuration, monkeypatch):
    servers = [
        {
            "name": "github",
            "url": "https://workspace.example/api/2.0/mcp/github",
            "clients": ["claude"],
        }
    ]
    configure_cowork = Mock()
    monkeypatch.setattr(desktop_setup, "configure_claude_desktop", lambda *a, **k: None)
    monkeypatch.setattr(desktop_setup, "configure_claude_cowork", configure_cowork)
    monkeypatch.setattr(cli, "_configure_managed_mcp_servers", lambda managed: servers)
    monkeypatch.setattr(cli, "_configure_managed_skills", lambda managed: True)
    monkeypatch.setattr(
        cli,
        "refresh_managed_config",
        lambda *a, **k: ({"enabled_agents": {"claude": {}}}, False),
    )

    assert (
        cli._configure_workspace_command(
            workspaces=[("https://workspace.example", "chosen-profile")]
        )
        == 0
    )
    configure_cowork.assert_called_once_with(configuration, servers)


@pytest.mark.parametrize("agents", [("claude",), ("claude", "codex"), ("codex", "claude")])
@pytest.mark.parametrize(
    "was_relayed,relayed", [(False, True), (True, False), (True, True), (False, False)]
)
def test_managed_cowork_uses_configured_claude_relay_state(
    configuration, monkeypatch, agents, was_relayed, relayed
):
    if was_relayed:
        configuration["claude_relayed"] = True
        configuration["relayed_proxy_port"] = 43210
    configured_inputs = {}

    def configure_agent(state, tools, **kwargs):
        tool = tools[0]
        configured_inputs[tool] = dict(state)
        configured = dict(state)
        configured["last_configured_tools"] = [tool]
        if tool == "claude":
            if relayed:
                configured["claude_relayed"] = True
            else:
                configured.pop("claude_relayed", None)
                configured.pop("relayed_proxy_port", None)
        return configured

    configure_cowork = Mock()
    monkeypatch.setattr(cli, "configure_selected_tools", configure_agent)
    monkeypatch.setattr(desktop_setup, "configure_claude_desktop", lambda *a, **k: None)
    monkeypatch.setattr(desktop_setup, "configure_claude_cowork", configure_cowork)
    monkeypatch.setattr(
        cli,
        "refresh_managed_config",
        lambda *a, **k: ({"enabled_agents": {tool: {} for tool in agents}}, False),
    )

    assert (
        cli._configure_workspace_command(
            workspaces=[("https://workspace.example", "chosen-profile")]
        )
        == 0
    )
    synced_state = configure_cowork.call_args.args[0]
    expected_port = 43210 if was_relayed and relayed else None
    assert bool(synced_state.get("claude_relayed")) is relayed
    assert synced_state.get("relayed_proxy_port") == expected_port
    if agents == ("claude", "codex"):
        assert bool(configured_inputs["codex"].get("claude_relayed")) is relayed
        assert configured_inputs["codex"].get("relayed_proxy_port") == expected_port


@pytest.mark.parametrize("failed_sync", ["mcp", "skills"])
def test_managed_sync_failure_preserves_existing_cowork(configuration, monkeypatch, failed_sync):
    configure_cowork = Mock()
    monkeypatch.setattr(desktop_setup, "configure_claude_desktop", lambda *a, **k: None)
    monkeypatch.setattr(desktop_setup, "configure_claude_cowork", configure_cowork)
    monkeypatch.setattr(
        cli,
        "_configure_managed_mcp_servers",
        lambda managed: None if failed_sync == "mcp" else [],
    )
    monkeypatch.setattr(cli, "_configure_managed_skills", lambda managed: failed_sync != "skills")
    monkeypatch.setattr(
        cli,
        "refresh_managed_config",
        lambda *a, **k: ({"enabled_agents": {"claude": {}}}, False),
    )

    assert (
        cli._configure_workspace_command(
            workspaces=[("https://workspace.example", "chosen-profile")]
        )
        == 0
    )
    configure_cowork.assert_not_called()


def test_unmanaged_configure_syncs_user_mcp_servers_to_cowork(configuration, monkeypatch):
    user_servers = [
        {
            "name": "system-ai-github",
            "url": "https://workspace.example/api/2.0/mcp/system.ai.github",
            "clients": ["claude"],
        }
    ]
    configuration["mcp_servers"] = user_servers
    configure_cowork = Mock()
    monkeypatch.setattr(desktop_setup, "configure_claude_desktop", lambda *a, **k: None)
    monkeypatch.setattr(desktop_setup, "configure_claude_cowork", configure_cowork)
    monkeypatch.setattr(cli, "load_state", lambda: configuration)

    assert (
        cli._configure_workspace_command(
            selected_tools=["claude"],
            workspaces=[("https://workspace.example", "chosen-profile")],
        )
        == 0
    )
    configure_cowork.assert_called_once_with(configuration, user_servers)


def test_managed_config_without_claude_preserves_cowork(configuration, monkeypatch):
    configuration["last_configured_tools"] = ["codex"]
    revert_cowork = Mock(return_value="removed")
    monkeypatch.setattr(desktop_setup, "revert_claude_cowork", revert_cowork)
    monkeypatch.setattr(
        cli,
        "refresh_managed_config",
        lambda *a, **k: ({"enabled_agents": {"codex": {}}}, False),
    )

    assert (
        cli._configure_workspace_command(
            workspaces=[("https://workspace.example", "chosen-profile")]
        )
        == 0
    )
    revert_cowork.assert_not_called()
