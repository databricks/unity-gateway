"""Configure scope selects real Desktop writers with external services isolated."""

import json
import shutil
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from tests.test_desktop_bundle import system_source as system_source
from tests.test_desktop_mdm import WORKSPACE
from tests.test_desktop_mdm import device as device
from ucode import cli, config_io, managed_files, workspace
from ucode.agents.desktop import claude, cowork, mdm, setup


@pytest.fixture
def desktop_configuration(device, monkeypatch):
    # Exercise Desktop's writers; CLI-agent setup and workspace discovery are separate boundaries.
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    state = {
        "workspace": WORKSPACE,
        "profile": "ADMIN_PROFILE",
        "available_tools": ["claude"],
        "last_configured_tools": ["claude"],
        "mcp_servers": [{"name": "slack", "url": f"{WORKSPACE}/mcp/slack", "clients": ["claude"]}],
    }
    monkeypatch.setattr(setup, "sys", SimpleNamespace(platform="darwin", executable=sys.executable))
    monkeypatch.setattr(claude, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(claude, "_claude_desktop_installed", lambda: True)
    monkeypatch.setattr(cli.claude_agent, "_managed_settings_path", lambda: None)
    monkeypatch.setattr(setup, "get_databricks_token", device.auth)
    monkeypatch.setattr(cli, "_configure_shared_workspace_states", lambda *a, **kw: [state])
    monkeypatch.setattr(cli, "refresh_managed_config", lambda *a, **kw: (None, False))
    monkeypatch.setattr(cli, "configure_selected_tools", lambda *a, **kw: state)
    monkeypatch.setattr(cli, "configure_single_tool", lambda *a, **kw: state)
    monkeypatch.setattr(cli, "save_state", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "install_databricks_cli", lambda: None)
    monkeypatch.setattr(cli, "install_tool_binary", lambda *a, **kw: True)
    monkeypatch.setattr(cli, "check_gateway_endpoint", lambda *a, **kw: True)
    monkeypatch.setattr(cli, "install_databricks_ai_tools_for_agents", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "_configure_managed_mcp_servers", lambda *a, **kw: [])
    monkeypatch.setattr(cli, "_configure_managed_skills", lambda *a, **kw: True)
    return state


def _configure(*flags):
    return CliRunner().invoke(
        cli.app, ["configure", "--agents", "claude", "--workspace", WORKSPACE, *flags]
    )


@pytest.mark.parametrize("flags", [[], ["--user"], ["--global"]], ids=["default", "user", "mdm"])
def test_desktop_scope_selects_profile_sources_and_registration(
    desktop_configuration, device, flags
):
    result = _configure(*flags)
    assert result.exit_code == 0, result.output
    assert not managed_files.machine_wide_only()

    mdm_only = flags == ["--global"]
    assert mdm.POLICY_PATH.exists() is mdm_only
    assert mdm.AGENT_PATH.exists() is mdm_only
    assert mdm.MDM_ROOT.exists() is mdm_only
    assert claude.claude_desktop_directory().exists() is not mdm_only
    assert cowork._marketplace_root().exists() is not mdm_only
    assert cowork._state_path().exists() is not mdm_only
    assert device.source.exists() is mdm_only
    root = cowork.discover_cowork_root()
    registry = root / "cowork_plugins/installed_plugins.json"
    if mdm_only:
        assert not registry.exists()
        assert not list(device.home.iterdir())
        plugin = device.source / "plugins/ug"
        assert (plugin / "skills/review/SKILL.md").read_bytes() == device.skill
        servers = json.loads((plugin / ".mcp.json").read_bytes())["mcpServers"]
        assert servers["slack"]["command"] == str(mdm.MDM_ROOT / "ug")
        return

    plugins = json.loads(registry.read_bytes())["plugins"]
    assert list(plugins) == [cowork.PLUGIN_ID]
    assert len(plugins[cowork.PLUGIN_ID]) == 1
    registration = config_io.read_json_safe(root / "cowork_plugins/known_marketplaces.json")
    source = cowork._marketplace_root()
    assert registration[cowork.MARKETPLACE_NAME]["source"]["path"] == str(source)


@pytest.mark.parametrize("flags", [[], ["--user"]], ids=["default", "user"])
def test_ordinary_configure_preserves_existing_mdm_deployment(desktop_configuration, device, flags):
    result = _configure("--global")
    assert result.exit_code == 0, result.output
    policy = mdm.POLICY_PATH.read_bytes()
    source = cowork._marketplace_files(device.source)
    helpers = cowork._marketplace_files(mdm.MDM_ROOT)
    agent = mdm.AGENT_PATH.read_bytes()

    result = _configure(*flags)

    assert result.exit_code == 0, result.output
    assert mdm.POLICY_PATH.read_bytes() == policy
    assert cowork._marketplace_files(device.source) == source
    assert cowork._marketplace_files(mdm.MDM_ROOT) == helpers
    assert mdm.AGENT_PATH.read_bytes() == agent
    assert claude.claude_desktop_directory().exists()
    assert (
        cowork._registered_marketplace_source(cowork.discover_cowork_root())
        == cowork._marketplace_root()
    )


def test_global_preserves_existing_desktop_user_files(desktop_configuration, device):
    result = _configure("--user")
    assert result.exit_code == 0, result.output
    profile = cowork._marketplace_files(claude.claude_desktop_directory())
    source = cowork._marketplace_files(cowork._marketplace_root())
    registration = cowork._marketplace_files(cowork.discover_cowork_root())
    state = cowork._state_path().read_bytes()

    result = _configure("--global")

    assert result.exit_code == 0, result.output
    assert mdm.POLICY_PATH.exists()
    assert cowork._marketplace_files(claude.claude_desktop_directory()) == profile
    assert cowork._marketplace_files(cowork._marketplace_root()) == source
    assert cowork._marketplace_files(cowork.discover_cowork_root()) == registration
    assert cowork._state_path().read_bytes() == state


@pytest.mark.parametrize("selector", [["--agent", "claude"], ["--agents", "claude,codex"]])
def test_global_provisions_without_a_desktop_account(
    desktop_configuration, device, tmp_path, selector
):
    shutil.rmtree(tmp_path / "Desktop")
    result = CliRunner().invoke(
        cli.app, ["configure", *selector, "--global", "--workspace", WORKSPACE]
    )
    assert result.exit_code == 0, result.output
    assert mdm.POLICY_PATH.exists()
    assert not claude.claude_desktop_directory().exists()
    assert not cowork._marketplace_root().exists()
    assert not (tmp_path / "Desktop").exists()


def test_global_extra_mcp_selection_applies_only_to_cli_clients(
    desktop_configuration, device, monkeypatch
):
    configure_mcp = Mock()
    monkeypatch.setattr(cli, "configure_mcp_command", configure_mcp)

    result = _configure("--global", "--mcp", "system.ai.gmail")

    assert result.exit_code == 0, result.output
    configure_mcp.assert_called_once_with(services={"system.ai.gmail"})
    plugin = device.source / "plugins/ug"
    servers = json.loads((plugin / ".mcp.json").read_bytes())["mcpServers"]
    assert set(servers) == {"slack"}
    assert servers["slack"]["command"] == str(mdm.MDM_ROOT / "ug")
    assert not cowork._marketplace_root().exists()


def test_global_provisioning_failure_fails_configure(desktop_configuration, device):
    device.admin = False
    result = _configure("--global")
    assert result.exit_code == 1
    assert "root from your MDM script" in " ".join(result.output.split())
    assert not mdm.POLICY_PATH.exists()
    assert not claude.claude_desktop_directory().exists()
    assert not cowork._marketplace_root().exists()
    assert not managed_files.machine_wide_only()


@pytest.mark.parametrize("selection", ["managed", "picker"])
@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_omitted_agents_validate_desktop_only_after_selection(
    desktop_configuration, device, monkeypatch, selection, tool
):
    device.admin = False
    desktop_configuration.update(available_tools=[tool], last_configured_tools=[tool])
    managed = {"enabled_agents": {tool: {}}} if selection == "managed" else None
    monkeypatch.setattr(cli, "refresh_managed_config", lambda *a, **kw: (managed, False))
    monkeypatch.setattr(cli, "prompt_for_tools", lambda *a: [tool])
    monkeypatch.setattr(cli, "_maybe_select_provider_service", lambda tool, state: state)
    configure = Mock(return_value=desktop_configuration)
    install = Mock(return_value=True)
    monkeypatch.setattr(cli, "configure_selected_tools", configure)
    monkeypatch.setattr(cli, "install_tool_binary", install)

    result = CliRunner().invoke(cli.app, ["configure", "--global", "--workspace", WORKSPACE])

    assert result.exit_code == (1 if tool == "claude" else 0), result.output
    if tool == "claude":
        assert "root from your MDM script" in " ".join(result.output.split())
        install.assert_not_called()
        configure.assert_not_called()
    else:
        install.assert_called_once_with("codex", strict=False)
        assert configure.call_args.args[1] == ["codex"]
    assert not mdm.MDM_ROOT.exists()
    assert not device.source.exists()
    assert not claude.claude_desktop_directory().exists()
    assert not cowork._marketplace_root().exists()
    assert not managed_files.machine_wide_only()


@pytest.mark.parametrize("field", ["use_pat", "custom_oauth"])
def test_global_rejects_unsupported_desktop_auth(desktop_configuration, device, field):
    desktop_configuration[field] = True
    result = _configure("--global")
    assert result.exit_code == 1
    assert "PAT/custom OAuth" in " ".join(result.output.split())
    device.auth.assert_not_called()
    assert not mdm.MDM_ROOT.exists()
    assert not claude.claude_desktop_directory().exists()
    assert not cowork._marketplace_root().exists()


@pytest.mark.parametrize(
    "options,diagnostic",
    [
        (["--profile", "PAT", "--use-pat"], "PAT/custom OAuth"),
        (
            [
                "--workspace",
                WORKSPACE,
                "--client-id",
                "custom-client",
                "--scopes",
                "all-apis,offline_access",
            ],
            "PAT/custom OAuth",
        ),
        (["--workspace", f"{WORKSPACE}?w=111"], "dedicated workspace hostname"),
    ],
)
def test_global_rejects_incompatible_requests_before_auth_or_agent_writes(
    device, monkeypatch, options, diagnostic
):
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(workspace, "host_type", lambda host: "unified")
    forbidden = Mock(
        side_effect=AssertionError("Invalid MDM requests must not authenticate or write")
    )
    monkeypatch.setattr(cli, "get_databricks_token", forbidden)
    monkeypatch.setattr(cli, "ensure_databricks_auth", forbidden)
    monkeypatch.setattr(cli, "run_databricks_login", forbidden)
    monkeypatch.setattr(cli, "configure_workspace_command", forbidden)
    monkeypatch.setattr(cli, "save_state", forbidden)
    monkeypatch.setattr(cli, "install_databricks_cli", lambda: None)
    result = CliRunner().invoke(cli.app, ["configure", "--global", "--agents", "claude", *options])
    assert result.exit_code == 1, result.output
    assert diagnostic in " ".join(result.output.split())
    forbidden.assert_not_called()
    device.auth.assert_not_called()
    assert not list(device.home.iterdir())
    assert not device.source.exists()
    assert not mdm.MDM_ROOT.exists()


def test_global_dry_run_writes_no_desktop_files(desktop_configuration, device):
    result = _configure("--global", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "Would publish Desktop policy" in result.output
    device.auth.assert_not_called()
    assert not mdm.MDM_ROOT.exists()
    assert not device.source.exists()
    assert not claude.claude_desktop_directory().exists()
    assert not cowork._marketplace_root().exists()


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_global_leaves_unsupported_desktop_platforms_unchanged(
    desktop_configuration, device, monkeypatch, platform
):
    monkeypatch.setattr(setup, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(cli.sys, "platform", platform)
    result = _configure("--global")
    assert result.exit_code == 0, result.output
    assert "supports macOS only" in " ".join(result.output.split())
    assert not mdm.MDM_ROOT.exists()
    assert not device.source.exists()
    assert not claude.claude_desktop_directory().exists()
    assert not cowork._marketplace_root().exists()
