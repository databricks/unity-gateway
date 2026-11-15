"""Configure scope selects real Desktop writers with external services isolated."""

import json
import plistlib
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
from ucode import state as state_store
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
    monkeypatch.setattr(
        cli,
        "configure_selected_tools",
        lambda state, tools, **kwargs: {**state, "last_configured_tools": tools},
    )
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
        plugin = device.source
        assert (plugin / "skills/review/SKILL.md").read_bytes() == device.skill
        servers = json.loads(plistlib.loads(mdm.POLICY_PATH.read_bytes())["managedMcpServers"])
        assert servers[0]["command"] == str(mdm.MDM_ROOT / "ug")
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

    result = _configure(*flags)

    assert result.exit_code == 0, result.output
    assert mdm.POLICY_PATH.read_bytes() == policy
    assert cowork._marketplace_files(device.source) == source
    assert cowork._marketplace_files(mdm.MDM_ROOT) == helpers
    assert claude.claude_desktop_directory().exists()
    assert not cowork._state_path().exists()


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


@pytest.mark.parametrize(
    "selector", [["--agent", "claude"], ["--agents", "claude-code"], ["--agents", "claude,codex"]]
)
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


def test_global_rejects_extra_user_mcp_selection(desktop_configuration, device, monkeypatch):
    configure_mcp = Mock()
    monkeypatch.setattr(cli, "configure_mcp_command", configure_mcp)

    result = _configure("--global", "--mcp", "system.ai.gmail")

    assert result.exit_code == 1, result.output
    assert "--mcp registers MCP servers in per-user settings" in " ".join(result.output.split())
    configure_mcp.assert_not_called()
    assert not mdm.POLICY_PATH.exists()
    assert not device.source.exists()
    assert not cowork._marketplace_root().exists()


@pytest.mark.parametrize("stage", ["preflight", "publication"])
def test_global_provisioning_failure_warns_and_keeps_cli_configuration(
    desktop_configuration, device, monkeypatch, stage
):
    if stage == "preflight":
        message = "Install Claude CLI in a shared machine location"
        monkeypatch.setattr(mdm, "_runtime", Mock(side_effect=RuntimeError(message)))
    else:
        message = "MDM publication failed"

        def publish(*args, **kwargs):
            assert device.auth.called
            raise RuntimeError(message)

        monkeypatch.setattr(mdm, "_publish", publish)

    configured = Mock(return_value=desktop_configuration)
    monkeypatch.setattr(cli, "configure_selected_tools", configured)
    result = _configure("--global")
    assert result.exit_code == 0, result.output
    assert configured.called
    assert "Could not configure Claude Desktop" in result.output
    assert message in " ".join(result.output.split())
    assert not mdm.POLICY_PATH.exists()
    assert not claude.claude_desktop_directory().exists()
    assert not cowork._marketplace_root().exists()
    assert not (cowork.discover_cowork_root() / "cowork_plugins/installed_plugins.json").exists()
    assert not managed_files.machine_wide_only()


@pytest.mark.parametrize("selection", ["managed", "picker"])
@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_omitted_agents_keep_configuring_when_desktop_prerequisites_fail(
    desktop_configuration, device, monkeypatch, selection, tool
):
    message = "Install Claude CLI in a shared machine location"
    monkeypatch.setattr(mdm, "_runtime", Mock(side_effect=RuntimeError(message)))
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

    assert result.exit_code == 0, result.output
    if tool == "claude":
        assert message in " ".join(result.output.split())
        assert "Could not configure Claude Desktop" in result.output
    else:
        assert "Could not configure Claude Desktop" not in result.output
    install.assert_called_once_with(tool, strict=False)
    assert configure.call_args.args[1] == [tool]
    assert not mdm.MDM_ROOT.exists()
    assert not device.source.exists()
    assert not claude.claude_desktop_directory().exists()
    assert not cowork._marketplace_root().exists()
    assert not managed_files.machine_wide_only()


@pytest.mark.parametrize("field", ["use_pat", "custom_oauth"])
def test_global_skips_unsupported_desktop_auth(desktop_configuration, device, field):
    desktop_configuration[field] = True
    result = _configure("--global")
    assert result.exit_code == 0, result.output
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
def test_global_keeps_cli_configuration_for_unsupported_desktop_options(
    desktop_configuration, device, monkeypatch, options, diagnostic
):
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(workspace, "host_type", lambda host: "unified")
    if "--use-pat" in options:
        desktop_configuration["use_pat"] = True
        monkeypatch.setattr(cli, "_parse_profile_option", lambda *args: [(WORKSPACE, "PAT")])
    elif "--client-id" in options:
        desktop_configuration["custom_oauth"] = {"client_id": "custom-client"}
    else:
        desktop_configuration["workspace"] = f"{WORKSPACE}?w=111"
    configured = Mock(return_value=desktop_configuration)
    monkeypatch.setattr(cli, "configure_selected_tools", configured)
    result = CliRunner().invoke(cli.app, ["configure", "--global", "--agents", "claude", *options])
    assert result.exit_code == 0, result.output
    assert diagnostic in " ".join(result.output.split())
    assert "Could not configure Claude Desktop" in result.output
    configured.assert_called_once()
    device.auth.assert_not_called()
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


@pytest.mark.parametrize("user_setup", [True, False], ids=["user-and-mdm", "mdm-only"])
@pytest.mark.parametrize("deny_elevation", [True, False], ids=["sudo-denied", "sudo-allowed"])
def test_plain_revert_cleans_available_scopes_and_retries_mdm(
    desktop_configuration, device, monkeypatch, user_setup, deny_elevation
):
    if user_setup:
        configured = _configure()
        assert configured.exit_code == 0, configured.output
    configured = _configure("--global")
    assert configured.exit_code == 0, configured.output
    policy_before = mdm.POLICY_PATH.read_bytes()
    if not user_setup:
        monkeypatch.setattr(claude, "_claude_desktop_installed", lambda: False)
    monkeypatch.setattr(state_store, "APP_DIR", state_store.STATE_PATH.parent)
    state_store.save_state(desktop_configuration)
    # Keep other agents' cleanup off host settings while checking they still run.
    restore = Mock(return_value=False)
    monkeypatch.setattr(cli, "restore_file", restore)
    monkeypatch.setattr(cli, "revert_mcp_configs", lambda state: {})
    monkeypatch.setattr(cli, "revert_legacy_shared_config", lambda: False)
    monkeypatch.setattr(cli.claude_agent, "revert_managed_settings", lambda: "unchanged")
    monkeypatch.setattr(cli.codex_agent, "revert_managed_config", lambda: "unchanged")

    with monkeypatch.context() as elevation:
        if deny_elevation:
            elevation.setattr(
                managed_files.subprocess_cross_os,
                "popen",
                Mock(side_effect=PermissionError("sudo authentication denied")),
            )
        result = CliRunner().invoke(cli.app, ["revert"])
    assert result.exit_code == 0, result.output
    assert restore.called
    assert not cowork._marketplace_root().exists()
    assert not (claude.claude_desktop_directory() / f"{claude.PROFILE_ID}.json").exists()
    if deny_elevation:
        output = " ".join(result.output.split())
        assert "an administrator must run `ug revert`" in output
        assert "sudo authentication denied" in output
        assert mdm.POLICY_PATH.read_bytes() == policy_before
        assert device.source.exists()
        retry = CliRunner().invoke(cli.app, ["revert"])
        assert retry.exit_code == 0, retry.output
    assert state_store.load_state() == {}
    assert not mdm.POLICY_PATH.exists()
    assert not mdm.MDM_ROOT.exists()
    assert not device.source.exists()
    repeated = CliRunner().invoke(cli.app, ["revert"])
    assert repeated.exit_code == 0, repeated.output


def test_global_configure_can_request_elevation_when_not_root(
    desktop_configuration, device, monkeypatch
):
    monkeypatch.setattr(managed_files.system_cross_os, "is_admin", lambda: False)
    monkeypatch.setattr(managed_files, "_running_as_root", lambda: False)
    result = _configure("--global")
    assert result.exit_code == 0, result.output
    assert mdm.POLICY_PATH.is_file()
    assert device.source.is_dir()
    assert not cowork._marketplace_root().exists()
