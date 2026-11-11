"""Desktop hooks on successful configure paths, not ordinary agent launches."""

import json
from typing import Any
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from ucode import cli, managed_files
from ucode.agents.desktop import claude as desktop
from ucode.agents.desktop import cowork
from ucode.agents.desktop import setup as desktop_setup
from ucode.agents.desktop.setup import configure_claude_cowork
from ucode.cli import _configure_managed_mcp_servers, _configure_managed_skills
from ucode.databricks import AnthropicModelCatalog
from ucode.mcp import McpServiceListingRateLimited


@pytest.mark.parametrize("flags", [[], ["--user"], ["--global"]])
@pytest.mark.parametrize("failed", [False, True])
def test_explicit_global_desktop_scope_is_command_local(monkeypatch, flags, failed):
    selections = []
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(cli, "install_databricks_cli", lambda: None)

    def configure_workspace(*args, **kwargs):
        selections.append(managed_files.machine_wide_only())
        if failed:
            raise RuntimeError("workspace unavailable")
        return 0

    monkeypatch.setattr(cli, "_configure_workspace_command", configure_workspace)
    result = CliRunner().invoke(
        cli.app,
        ["configure", "--agents", "claude", "--workspace", "https://workspace.example", *flags],
    )
    assert result.exit_code == (1 if failed else 0), result.output
    assert selections == [flags == ["--global"]]
    assert not managed_files.machine_wide_only()


def test_global_skips_user_desktop_setup(monkeypatch):
    from ucode.agents.desktop import mdm

    managed_files.set_machine_wide_only(True)
    machine_configure = Mock()
    monkeypatch.setattr(mdm, "configure", machine_configure)

    def unexpected_write(*args, **kwargs):
        pytest.fail("--global attempted user-level Desktop setup")

    monkeypatch.setattr(desktop_setup, "write_claude_desktop_profile", unexpected_write)
    monkeypatch.setattr(desktop_setup, "configure_cowork", unexpected_write)
    monkeypatch.setattr(desktop_setup, "configure_code", unexpected_write)
    monkeypatch.setattr(desktop_setup.sys, "platform", "darwin")
    desktop_setup.configure_claude_desktop({"workspace": "https://workspace.example"})
    desktop_setup.configure_claude_cowork({}, [])
    desktop_setup.configure_claude_code_plugin([])
    machine_configure.assert_called_once_with("https://workspace.example", None)


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_global_skips_desktop_mdm_on_other_platforms(monkeypatch, platform):
    from ucode.agents.desktop import mdm

    managed_files.set_machine_wide_only(True)
    monkeypatch.setattr(desktop_setup.sys, "platform", platform)
    machine_configure = Mock()
    profile_write = Mock()
    monkeypatch.setattr(mdm, "configure", machine_configure)
    monkeypatch.setattr(desktop_setup, "write_claude_desktop_profile", profile_write)
    desktop_setup.configure_claude_desktop({"workspace": "https://workspace.example"})
    machine_configure.assert_not_called()
    profile_write.assert_not_called()


def test_global_desktop_provisioning_failure_warns(monkeypatch, capsys):
    message = "Could not update Desktop plugin: sudo authentication failed"
    from ucode.agents.desktop import mdm

    managed_files.set_machine_wide_only(True)
    monkeypatch.setattr(desktop_setup.sys, "platform", "darwin")
    monkeypatch.setattr(mdm, "configure", Mock(side_effect=RuntimeError(message)))
    profile_write = Mock()
    monkeypatch.setattr(desktop_setup, "write_claude_desktop_profile", profile_write)
    desktop_setup.configure_claude_desktop({"workspace": "https://workspace.example"})
    output = " ".join(capsys.readouterr().out.split())
    assert "Could not configure Claude Desktop" in output
    assert message in output
    assert "Claude Code setup is unchanged" in output
    profile_write.assert_not_called()


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
    monkeypatch.setattr(desktop_setup, "configure_claude_code_plugin", lambda *a, **k: None)
    monkeypatch.setattr(desktop_setup, "configure_claude_cowork", lambda *a, **k: None)
    monkeypatch.setattr(desktop_setup, "revert_claude_cowork", lambda *a, **k: "unchanged")
    return state


@pytest.mark.parametrize("flow", ["single", "managed", "selected"])
def test_successful_claude_setup_configures_desktop_once(configuration, monkeypatch, flow):
    configure = Mock()
    monkeypatch.setattr(desktop_setup, "configure_claude_desktop", configure)
    args: dict[str, Any] = {"selected_tools": ["claude"]}
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
    configure_code = Mock()
    configure_mcps = Mock(return_value=servers)
    monkeypatch.setattr(desktop_setup, "configure_claude_desktop", lambda *a, **k: None)
    monkeypatch.setattr(desktop_setup, "configure_claude_cowork", configure_cowork)
    monkeypatch.setattr(desktop_setup, "configure_claude_code_plugin", configure_code)
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
    configure_code.assert_called_once_with(servers)
    configure_cowork.assert_called_once_with(configuration, servers, sync_plugin=True)


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
    configure_cowork.assert_called_once_with(configuration, [], sync_plugin=True)


@pytest.mark.parametrize("flow", ["single", "managed", "selected"])
def test_desktop_failure_preserves_successful_configure_exit(
    configuration, monkeypatch, capsys, flow
):
    def fail(*a, **k):
        raise OSError("Desktop profile is not writable")

    monkeypatch.setattr(desktop_setup, "_configure_claude_desktop_user_level", fail)
    monkeypatch.setattr(desktop_setup.sys, "platform", "darwin")
    args: dict[str, Any] = {"selected_tools": ["claude"]}
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
    configure_code = Mock()
    monkeypatch.setattr(desktop_setup, "configure_claude_desktop", lambda *a, **k: None)
    monkeypatch.setattr(desktop_setup, "configure_claude_cowork", configure_cowork)
    monkeypatch.setattr(desktop_setup, "configure_claude_code_plugin", configure_code)
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
    configure_code.assert_called_once_with(servers)
    configure_cowork.assert_called_once_with(configuration, servers, sync_plugin=True)


@pytest.mark.parametrize("flow", ["single", "managed"])
@pytest.mark.parametrize("failed_sync", ["mcp", "skills"])
def test_managed_sync_failure_preserves_existing_cowork(
    configuration, monkeypatch, failed_sync, flow
):
    servers = [{"name": "system-ai-github", "clients": ["claude"]}]
    configure_cowork = Mock()
    configure_code = Mock()
    monkeypatch.setattr(desktop_setup, "configure_claude_desktop", lambda *a, **k: None)
    monkeypatch.setattr(desktop_setup, "configure_claude_cowork", configure_cowork)
    monkeypatch.setattr(desktop_setup, "configure_claude_code_plugin", configure_code)
    monkeypatch.setattr(
        cli,
        "_configure_managed_mcp_servers",
        lambda managed: None if failed_sync == "mcp" else servers,
    )
    monkeypatch.setattr(cli, "_configure_managed_skills", lambda managed: failed_sync != "skills")
    monkeypatch.setattr(
        cli,
        "refresh_managed_config",
        lambda *a, **k: ({"enabled_agents": {"claude": {}}}, False),
    )

    assert (
        cli._configure_workspace_command(
            workspaces=[("https://workspace.example", "chosen-profile")],
            **({"tool": "claude"} if flow == "single" else {}),
        )
        == 0
    )
    if failed_sync == "mcp":
        configure_cowork.assert_not_called()
        configure_code.assert_not_called()
    else:
        configure_cowork.assert_called_once_with(configuration, servers, sync_plugin=False)
        configure_code.assert_called_once_with(servers)


@pytest.mark.parametrize("flow", ["single", "managed"])
@pytest.mark.parametrize("remove_connectors", [False, True], ids=["replace", "remove"])
@pytest.mark.parametrize("failure", ["skills", "rate-limit", "discovery-error"])
def test_reconfigure_preserves_connectors_until_refresh_succeeds(
    configuration, monkeypatch, tmp_path, capsys, flow, remove_connectors, failure
):
    """Failed discovery keeps old connectors; successful discovery applies replacements/removals."""
    directory = tmp_path / "Desktop" / "configLibrary"
    directory.mkdir(parents=True)
    monkeypatch.setattr(desktop, "claude_desktop_directory", lambda: directory)
    monkeypatch.setattr(desktop_setup, "claude_desktop_directory", lambda: directory)
    monkeypatch.setattr(desktop_setup, "get_databricks_token", lambda *args: "token")
    monkeypatch.setattr(
        desktop_setup,
        "list_anthropic_model_catalog",
        lambda *args, **kwargs: AnthropicModelCatalog(
            model_ids=["system.ai.claude-haiku-4-5"], model_id_to_display_name={}
        ),
    )
    monkeypatch.setattr(desktop_setup, "configure_claude_cowork", configure_claude_cowork)
    monkeypatch.setattr(cli, "_configure_managed_skills", _configure_managed_skills)
    monkeypatch.setattr(cowork.shutil, "which", lambda name: str(tmp_path / "bin" / name))

    def fail_download(*args):
        raise RuntimeError("skill download unavailable")

    def unexpected_plugin_refresh(*args, **kwargs):
        pytest.fail("Failed skill downloads must preserve the installed plugin")

    monkeypatch.setattr(cli, "reconcile_managed_skills", fail_download)
    monkeypatch.setattr(cowork, "records_for_scope", unexpected_plugin_refresh)
    monkeypatch.setattr(cowork.subprocess_cross_os, "run", unexpected_plugin_refresh)
    marketplace = cowork._marketplace_root()
    marketplace.mkdir(parents=True)
    (marketplace / "existing-skill.md").write_text("Keep the installed skill")
    before_plugin = cowork._marketplace_files(marketplace)
    desktop_setup.configure_claude_desktop(configuration)
    with desktop.desktop_mcp_configuration(
        configuration["workspace"], {"mcpServers": {"old": {"command": "/old-proxy", "args": []}}}
    ):
        pass
    servers = (
        []
        if remove_connectors
        else [
            {
                "name": "system-ai-github",
                "url": "https://workspace.example/ai-gateway/mcp-services/system.ai.github",
                "clients": ["claude"],
            }
        ]
    )
    monkeypatch.setattr(cli, "_configure_managed_mcp_servers", lambda managed: servers)
    if failure != "skills":
        monkeypatch.setattr(cli, "_configure_managed_mcp_servers", _configure_managed_mcp_servers)

        def fail_discovery(*args):
            if failure == "rate-limit":
                raise McpServiceListingRateLimited("system.ai")
            raise RuntimeError("connector discovery unavailable")

        monkeypatch.setattr(cli, "reconcile_managed_mcp_servers", fail_discovery)
    monkeypatch.setattr(
        cli,
        "refresh_managed_config",
        lambda *args, **kwargs: ({"enabled_agents": {"claude": {}}}, False),
    )

    assert (
        cli._configure_workspace_command(
            workspaces=[("https://workspace.example", "chosen-profile")],
            **({"tool": "claude"} if flow == "single" else {}),
        )
        == 0
    )

    profile = json.loads((directory / f"{desktop.PROFILE_ID}.json").read_text())
    connectors = profile["managedMcpServers"]
    if failure != "skills":
        assert connectors == [
            {"name": "old", "transport": "stdio", "command": "/old-proxy", "args": []}
        ]
    else:
        assert [server["name"] for server in connectors] == (
            [] if remove_connectors else ["github"]
        )
        if connectors:
            assert connectors[0]["transport"] == "stdio"
            assert connectors[0]["command"] == str(tmp_path / "bin" / "ug")
            assert servers[0]["url"] in connectors[0]["args"]
    assert cowork._marketplace_files(marketplace) == before_plugin
    assert "skill download unavailable" in capsys.readouterr().out


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
    configure_cowork.assert_called_once_with(configuration, user_servers, sync_plugin=True)


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
