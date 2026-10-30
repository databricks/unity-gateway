"""MDM filesystem lifecycle with external workspace, launchd, and native Claude boundaries replaced."""

import json
import os
import plistlib
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from tests.test_desktop_marketplace import system_source as system_source
from ucode import cli, config_io, desktop_auth, mcp, skills_download
from ucode.agents.desktop import claude_config, cowork, mdm, setup, system_marketplace
from ucode.databricks import AnthropicModelCatalog
from ucode.managed_config import FetchedManagedConfig
from ucode.skills_api import SkillRef

WORKSPACE = "https://workspace.example"
SKILL = SkillRef("main", "team", "review", "review")
RUNTIME = {
    "python": "/opt/unitygateway/venv/bin/python",
    "claude": "/opt/unitygateway/bin/claude",
    "databricks": "/opt/unitygateway/bin/databricks",
}


@pytest.fixture
def device(system_source, monkeypatch, tmp_path):
    tmp_path.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(mdm, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(mdm, "MDM_ROOT", tmp_path / "machine/mdm")
    monkeypatch.setattr(mdm, "POLICY_PATH", tmp_path / "machine/policy.plist")
    monkeypatch.setattr(mdm, "AGENT_PATH", tmp_path / "machine/agents/ug.plist")
    # These temporary directories belong to the test runner, not root. Validate ownership separately.
    monkeypatch.setattr(mdm, "_check_machine_path", lambda *a, **kw: None)
    monkeypatch.setattr(mdm, "_runtime", lambda: dict(RUNTIME))
    activate = Mock()
    monkeypatch.setattr(mdm, "_activate_logged_in_user", activate)
    state = SimpleNamespace(
        admin=True,
        source=system_source,
        activate=activate,
        home=home,
        manifest={
            "enabled_agents": {"claude": {"otel_tracing_enabled": True}},
            "mcp_servers": {"names": ["system.ai.slack"]},
            "skills": {"names": [SKILL.fqn]},
        },
        skill=b"---\nname: review\ndescription: Review code\n---\nCheck tests.\n",
    )
    monkeypatch.setattr(mdm.system_cross_os, "is_admin", lambda: state.admin)
    state.auth = Mock(return_value="provisioning-secret")
    monkeypatch.setattr(mdm.databricks, "get_databricks_token", state.auth)
    monkeypatch.setattr(
        mdm, "get_managed_config", lambda *a: FetchedManagedConfig(state.manifest, None)
    )
    monkeypatch.setattr(
        setup,
        "list_anthropic_model_catalog",
        lambda *a, **kw: AnthropicModelCatalog(
            model_ids=["system.ai.claude-sonnet-4-6", "system.ai.gpt-5"],
            model_id_to_display_name={},
        ),
    )
    monkeypatch.setattr(skills_download, "get_skill", lambda *a: SKILL)
    monkeypatch.setattr(
        skills_download,
        "fetch_skill_bundle",
        lambda *a: ({"SKILL.md": state.skill, "asset.bin": b"\x00\xff"}, None),
    )
    return state


def _installed(root):
    registry = config_io.read_json_safe(root / "cowork_plugins/installed_plugins.json")
    assert list(registry["plugins"]) == [cowork.PLUGIN_ID]
    entries = registry["plugins"][cowork.PLUGIN_ID]
    assert len(entries) == 1
    return Path(entries[0]["installPath"])


def _snapshot(device):
    return {
        "source": system_marketplace.read_bundle(device.source),
        "mdm": system_marketplace.read_bundle(mdm.MDM_ROOT),
        "policy": mdm.POLICY_PATH.read_bytes(),
        "agent": mdm.AGENT_PATH.read_bytes(),
    }


def test_provision_without_account_has_no_admin_credentials_or_user_writes(device, tmp_path):
    shutil.rmtree(tmp_path / "Desktop")
    mdm.configure(WORKSPACE, "ADMIN_PROFILE")
    policy = plistlib.loads(mdm.POLICY_PATH.read_bytes())
    assert all(isinstance(value, str) for value in policy.values())
    assert policy["inferenceGatewayBaseUrl"] == f"{WORKSPACE}/ai-gateway/anthropic"
    assert json.loads(policy["inferenceModels"]) == [{"name": "system.ai.claude-sonnet-4-6"}]
    assert policy["modelDiscoveryEnabled"] == "false"
    assert policy["otlpTracesEnabled"] == "true"
    assert policy["inferenceCredentialHelperTimeoutSec"] == "300"
    descriptor = json.loads((mdm.MDM_ROOT / "deployment.json").read_bytes())
    assert descriptor["profile"].startswith("UG_DESKTOP_")
    assert json.loads(policy["inferenceCredentialHelperArgs"]) == [
        "claude-desktop-auth",
        "--host",
        WORKSPACE,
        "--profile",
        descriptor["profile"],
        "--mdm-desktop",
    ]
    plugin = device.source / "plugins/ug"
    servers = json.loads((plugin / ".mcp.json").read_bytes())["mcpServers"]
    assert list(servers) == ["slack"]
    assert servers["slack"]["command"] == str(mdm.MDM_ROOT / "ug")
    assert (plugin / "skills/review/SKILL.md").read_bytes() == device.skill
    assert (plugin / "skills/review/asset.bin").read_bytes() == b"\x00\xff"
    assert (plugin / "skills/unity-gateway-connectors/SKILL.md").is_file()
    assert (plugin / "hooks/hooks.json").is_file()
    assert not (plugin / "version.json").exists()
    for bundle in (
        system_marketplace.read_bundle(device.source),
        system_marketplace.read_bundle(mdm.MDM_ROOT),
    ):
        for data, _ in bundle.values():
            assert b"provisioning-secret" not in data
            assert b"ADMIN_PROFILE" not in data
            assert str(device.home).encode() not in data
    assert not list(device.home.iterdir())
    assert not cowork._marketplace_root().exists()
    assert not cowork._state_path().exists()
    agent = plistlib.loads(mdm.AGENT_PATH.read_bytes())
    assert agent["ProgramArguments"] == [str(mdm.MDM_ROOT / "ug"), "desktop-mdm-sync"]
    assert agent["RunAtLoad"] is True and agent["StartInterval"] == 60
    if os.name != "nt":
        assert (mdm.MDM_ROOT / "state.json").stat().st_mode & 0o777 == 0o600
        assert (mdm.MDM_ROOT / "ug").stat().st_mode & 0o777 == 0o755
    device.auth.assert_called_once_with(WORKSPACE, "ADMIN_PROFILE", browser_login=True)
    device.admin = False
    assert mdm.sync_user() is False  # An account appears only when the user opens Cowork.
    device.auth.assert_called_once()


def test_sync_update_and_remove_keep_one_registration_without_authentication(device):
    mdm.configure(WORKSPACE, "admin")
    device.auth.reset_mock()
    device.admin = False
    root = cowork.discover_cowork_root()
    assert mdm.sync_user()
    installed = _installed(root)
    assert system_marketplace.read_bundle(installed) == system_marketplace.read_bundle(
        device.source / "plugins/ug"
    )
    assert cowork._registered_marketplace_source(root) == device.source
    assert mdm.sync_user() is False
    device.auth.assert_not_called()

    device.admin = True
    device.skill = device.skill.replace(b"Check tests.", b"Updated verification code.")
    device.manifest["mcp_servers"]["names"].append("system.ai.github")
    device.manifest["enabled_agents"]["claude"]["otel_tracing_enabled"] = False
    mdm.configure(WORKSPACE, "admin")
    assert "otlpEndpoint" not in plistlib.loads(mdm.POLICY_PATH.read_bytes())
    assert not (mdm.MDM_ROOT / "otel-headers.sh").exists()
    assert (installed / "skills/review/SKILL.md").read_bytes() != device.skill
    device.auth.reset_mock()
    device.admin = False
    assert mdm.sync_user()
    assert (_installed(root) / "skills/review/SKILL.md").read_bytes() == device.skill
    assert len(json.loads((_installed(root) / ".mcp.json").read_bytes())["mcpServers"]) == 2
    device.auth.assert_not_called()

    device.admin = True
    mdm.remove()
    assert not device.source.exists() and not mdm.POLICY_PATH.exists()
    assert mdm.AGENT_PATH.exists() and (mdm.MDM_ROOT / "ug").exists()
    assert json.loads((mdm.MDM_ROOT / "deployment.json").read_bytes())["enabled"] is False
    device.admin = False
    assert mdm.sync_user() is False
    assert cowork._registered_marketplace_source(root) is None
    assert not (device.home / ".ucode" / mdm.USER_STATE_FILENAME).exists()
    device.auth.assert_not_called()


def test_sync_migrates_user_source_and_preserves_unrelated_plugin(device):
    mdm.configure(WORKSPACE, "admin")
    user_source = cowork._marketplace_root()
    shutil.copytree(device.source, user_source)
    root = cowork.discover_cowork_root()
    cowork._install_plugin(root, user_source)
    registry_path = root / "cowork_plugins/installed_plugins.json"
    registry = config_io.read_json_safe(registry_path)
    registry["plugins"]["personal@local"] = [{"installPath": "/personal/plugin"}]
    config_io.atomic_write_json(registry_path, registry)
    device.admin = False
    mdm.sync_user()
    registry = config_io.read_json_safe(registry_path)
    assert set(registry["plugins"]) == {cowork.PLUGIN_ID, "personal@local"}
    assert len(registry["plugins"][cowork.PLUGIN_ID]) == 1
    assert cowork._registered_marketplace_source(root) == device.source
    assert user_source.exists()


@pytest.mark.parametrize(
    "failure", ["fetch", "malformed_mcp", "missing_skill", "download", "file_write"]
)
def test_failed_update_preserves_previous_deployment(device, monkeypatch, failure):
    mdm.configure(WORKSPACE, "admin")
    before = _snapshot(device)
    device.skill += b"New version.\n"
    if failure == "fetch":
        monkeypatch.setattr(
            mdm, "get_managed_config", lambda *a: FetchedManagedConfig(None, "denied")
        )
    elif failure == "malformed_mcp":
        device.manifest["mcp_servers"]["names"].append("bad-name")
    elif failure == "missing_skill":
        monkeypatch.setattr(skills_download, "get_skill", lambda *a: None)
    elif failure == "download":
        monkeypatch.setattr(skills_download, "fetch_skill_bundle", lambda *a: (None, "unavailable"))
    else:
        original = claude_config._write_or_remove

        def fail_once(path, contents, *, mode):
            if path == mdm.POLICY_PATH:
                monkeypatch.setattr(claude_config, "_write_or_remove", original)
                raise OSError("test publication failure")
            return original(path, contents, mode=mode)

        # Force a policy change as well as a bundle change.
        device.manifest["enabled_agents"]["claude"]["otel_tracing_enabled"] = False
        monkeypatch.setattr(claude_config, "_write_or_remove", fail_once)
    with pytest.raises(RuntimeError):
        mdm.configure(WORKSPACE, "admin")
    assert _snapshot(device) == before


@pytest.mark.parametrize("target", ["policy", "source"])
def test_foreign_policy_or_marketplace_is_never_adopted(device, target):
    path = mdm.POLICY_PATH if target == "policy" else device.source / "foreign.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o755)
    path.write_text("IT-owned")
    with pytest.raises(RuntimeError, match="not this"):
        mdm.configure(WORKSPACE, "admin")
    assert path.read_text() == "IT-owned"


def test_partial_refresh_can_be_removed_after_account_switch(device, monkeypatch, tmp_path):
    mdm.configure(WORKSPACE, "admin")
    device.admin = False
    old_root = cowork.discover_cowork_root()
    mdm.sync_user()
    new_root = tmp_path / "Desktop/local-agent-mode-sessions/new-account/new-org"
    new_root.mkdir(parents=True)
    monkeypatch.setattr(cowork, "discover_cowork_root", lambda: new_root)
    native = cowork.subprocess_cross_os.run

    def fail_old_cleanup(argv, **kwargs):
        if kwargs["env"]["CLAUDE_CONFIG_DIR"] == str(old_root) and argv[2] == "uninstall":
            raise OSError("test native cleanup failure")
        return native(argv, **kwargs)

    monkeypatch.setattr(cowork.subprocess_cross_os, "run", fail_old_cleanup)
    with pytest.raises(OSError):
        mdm.sync_user()
    assert cowork._registered_marketplace_source(new_root) == device.source
    monkeypatch.setattr(cowork.subprocess_cross_os, "run", native)
    device.admin = True
    mdm.remove()
    device.admin = False
    mdm.sync_user()
    assert cowork._registered_marketplace_source(old_root) is None
    assert cowork._registered_marketplace_source(new_root) is None


def test_removal_preserves_later_independent_root_installation(device):
    mdm.configure(WORKSPACE, "admin")
    device.admin = False
    mdm.sync_user()
    root = cowork.discover_cowork_root()
    manifest = _installed(root) / ".claude-plugin/plugin.json"
    contents = json.loads(manifest.read_bytes())
    contents["version"] = "0.0.0-independent-user-install"
    manifest.write_text(json.dumps(contents))
    device.admin = True
    mdm.remove()
    device.admin = False
    mdm.sync_user()
    assert cowork._registered_marketplace_source(root) == device.source
    assert manifest.read_text() == json.dumps(contents)


def test_location_discovery_reuses_provisioning_auth(device, monkeypatch):
    device.manifest["mcp_servers"] = {"unity_catalog_location": "system.ai"}
    listing = Mock(return_value=(["system.ai.slack"], None))
    monkeypatch.setattr(mcp, "list_mcp_services", listing)
    token = Mock(side_effect=AssertionError("Do not authenticate again"))
    monkeypatch.setattr(mcp, "get_databricks_token", token)
    mdm.configure(WORKSPACE, "admin")
    device.auth.assert_called_once()
    listing.assert_called_once_with(WORKSPACE, "provisioning-secret", parent="system.ai")


def test_workspace_removals_clear_old_assets_and_connectors(device):
    mdm.configure(WORKSPACE, "admin")
    device.admin = False
    mdm.sync_user()
    device.admin = True
    device.manifest["skills"] = {}
    device.manifest["mcp_servers"] = {}
    mdm.configure(WORKSPACE, "admin")
    device.admin = False
    mdm.sync_user()
    plugin = _installed(cowork.discover_cowork_root())
    assert not (plugin / "skills/review").exists()
    assert not (plugin / "hooks/hooks.json").exists()
    assert not (plugin / ".mcp.json").exists()


@pytest.mark.parametrize("conflict", ["source", "org_plugin"])
def test_refresh_preserves_conflicting_installations(device, monkeypatch, tmp_path, conflict):
    mdm.configure(WORKSPACE, "admin")
    root = cowork.discover_cowork_root()
    other = tmp_path / "other-plugin"
    if conflict == "source":
        shutil.copytree(device.source, other)
        cowork._install_plugin(root, other)
    else:
        other.mkdir()
        monkeypatch.setattr(mdm.managed_files, "claude_desktop_org_plugin_path", lambda: other)
    before = system_marketplace.read_bundle(root)
    device.admin = False
    with pytest.raises(RuntimeError):
        mdm.sync_user()
    assert system_marketplace.read_bundle(root) == before
    assert other.exists()


def test_edited_owned_policy_blocks_update_and_removal(device):
    mdm.configure(WORKSPACE, "admin")
    mdm.POLICY_PATH.write_text("externally changed")
    before = _snapshot(device)
    for operation in (lambda: mdm.configure(WORKSPACE, "admin"), mdm.remove):
        with pytest.raises(RuntimeError, match="edited externally"):
            operation()
        assert _snapshot(device) == before


def test_dry_run_has_no_auth_discovery_or_writes(device):
    config_io.set_dry_run(True)
    mdm.configure(WORKSPACE, "admin")
    device.auth.assert_not_called()
    device.activate.assert_not_called()
    assert not device.source.exists() and not mdm.MDM_ROOT.exists()


@pytest.mark.parametrize(
    "platform,admin,provision",
    [
        ("linux", True, True),
        ("win32", True, True),
        ("darwin", False, True),
        ("darwin", True, False),
    ],
)
def test_platform_and_identity_guards(monkeypatch, platform, admin, provision):
    monkeypatch.setattr(mdm, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(mdm.system_cross_os, "is_admin", lambda: admin)
    with pytest.raises(RuntimeError):
        mdm._require_macos(administrator=provision)


@pytest.mark.parametrize("admin", [True, False])
def test_mdm_helper_uses_user_auth_and_never_runs_plugin_install(monkeypatch, admin):
    monkeypatch.setattr(mdm, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(mdm.system_cross_os, "is_admin", lambda: admin)
    auth = Mock(return_value="user-token")
    monkeypatch.setattr(desktop_auth, "get_claude_desktop_databricks_token", auth)
    monkeypatch.setattr(
        mdm, "sync_user", Mock(side_effect=AssertionError("auth must not wait on plugin install"))
    )
    result = CliRunner().invoke(
        cli.app,
        [
            "claude-desktop-auth",
            "--mdm-desktop",
            "--host",
            WORKSPACE,
            "--profile",
            "UG_DESKTOP_test",
        ],
    )
    if admin:
        assert result.exit_code == 1
        assert result.stdout == ""
        auth.assert_not_called()
    else:
        assert result.exit_code == 0
        assert result.stdout == "user-token\n"
        auth.assert_called_once_with(WORKSPACE, "UG_DESKTOP_test")


@pytest.mark.parametrize("private", [False, True])
def test_shared_runtime_checks_access_but_retains_virtualenv_path(monkeypatch, tmp_path, private):
    executable = tmp_path / "venv/bin/python"
    resolved = tmp_path / "system/python"
    monkeypatch.setattr(Path, "exists", lambda p: True)
    monkeypatch.setattr(Path, "is_file", lambda p: True)
    monkeypatch.setattr(Path, "is_dir", lambda p: False)
    monkeypatch.setattr(Path, "resolve", lambda p: resolved if p == executable else p)
    checked = Mock()
    monkeypatch.setattr(mdm, "_check_machine_path", checked)
    monkeypatch.setattr(
        Path,
        "stat",
        lambda p: SimpleNamespace(
            st_mode=stat.S_IFREG | (0o700 if private and p == executable.parent else 0o755)
        ),
    )
    if private:
        with pytest.raises(RuntimeError, match="accessible"):
            mdm._shared_path(str(executable), "Python", executable=True)
    else:
        assert mdm._shared_path(str(executable), "Python", executable=True) == executable
        checked.assert_any_call(resolved, required=True)
        checked.assert_any_call(executable.parent, required=True)


def test_isolated_runtime_must_import_the_provisioning_package(monkeypatch):
    monkeypatch.setattr(mdm, "_shared_path", lambda value, *a, **kw: Path(value))
    probe = Mock(
        return_value=SimpleNamespace(returncode=0, stdout="/other/ucode/agents/desktop/mdm.py")
    )
    monkeypatch.setattr(mdm.subprocess_cross_os, "run", probe)
    with pytest.raises(RuntimeError, match="shared Python environment"):
        mdm._runtime()
    assert probe.call_args.args[0][1:3] == ["-I", "-c"]


@pytest.mark.skipif(os.name == "nt", reason="macOS wrapper requires a POSIX shell")
def test_runtime_wrapper_discards_provisioning_credentials_and_preserves_user_context(tmp_path):
    runtime = dict(RUNTIME)
    executable = tmp_path / "shared python"
    # Observe the interpreter boundary without invoking an agent, API, or authentication.
    observe = "import json,os,sys; print(json.dumps({'env':dict(os.environ),'args':sys.argv[1:]}))"
    executable.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -c {shlex.quote(observe)} "$@"\n'
    )
    executable.chmod(0o755)
    runtime["python"] = str(executable)
    wrapper = tmp_path / "ug"
    wrapper.write_bytes(mdm._runtime_script(runtime))
    wrapper.chmod(0o755)
    overrides = dict.fromkeys(
        (
            "DATABRICKS_BEARER",
            "DATABRICKS_BEARER_COMMAND",
            "DATABRICKS_TOKEN",
            "DATABRICKS_CLIENT_SECRET",
            "DATABRICKS_CONFIG_FILE",
            "PYTHONHOME",
            "PYTHONPATH",
        ),
        "provisioner-only",
    )
    result = subprocess.run(
        [str(wrapper), "mcp-proxy", "--profile", "user profile"],
        env={**os.environ, **overrides, "CLAUDE_HELPER_CONTEXT": "interactive"},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    observed = json.loads(result.stdout)
    assert not (set(overrides) & set(observed["env"]))
    assert observed["env"]["CLAUDE_HELPER_CONTEXT"] == "interactive"
    assert observed["args"] == ["-I", "-m", "ucode.cli", "mcp-proxy", "--profile", "user profile"]
    assert observed["env"]["PATH"].split(":")[:2] == [str(mdm.MDM_ROOT), "/opt/unitygateway/bin"]


@pytest.mark.parametrize("problem", ["owner", "writable", "link", "missing"])
def test_machine_path_validation(monkeypatch, problem):
    path = Path("/machine/file")
    monkeypatch.setattr(Path, "is_symlink", lambda p: p == path and problem == "link")
    monkeypatch.setattr(Path, "exists", lambda p: not (p == path and problem == "missing"))
    monkeypatch.setattr(
        Path,
        "stat",
        lambda p: SimpleNamespace(
            st_uid=501 if p == path and problem == "owner" else 0,
            st_mode=0o100666 if p == path and problem == "writable" else 0o100644,
        ),
    )
    with pytest.raises(RuntimeError):
        mdm._check_machine_path(path, required=True)


@pytest.mark.parametrize(
    "args", [[], ["--user"], ["--agents", "codex"], ["--use-pat"], ["--enable-databricks-ai-tools"]]
)
def test_cli_rejects_incompatible_mdm_arguments(monkeypatch, args):
    configure = Mock()
    monkeypatch.setattr(mdm, "configure", configure)
    argv = ["configure", "--mdm-desktop", "--workspace", WORKSPACE, *args]
    if args and "--agents" not in args:
        argv += ["--agents", "claude"]
    result = CliRunner().invoke(cli.app, argv)
    assert result.exit_code == 1
    configure.assert_not_called()


def test_cli_dispatches_mdm_without_normal_agent_configuration(monkeypatch):
    configure = Mock()
    monkeypatch.setattr(mdm, "configure", configure)
    for name in ("install_databricks_cli", "_configure_workspace_command", "save_state"):
        monkeypatch.setattr(cli, name, Mock(side_effect=AssertionError(name)))
    result = CliRunner().invoke(
        cli.app,
        [
            "configure",
            "--agents",
            "claude",
            "--mdm-desktop",
            "--workspace",
            WORKSPACE,
            "--mcp",
            "system.ai.slack",
        ],
    )
    assert result.exit_code == 0, result.output
    configure.assert_called_once_with(WORKSPACE, None, mcp_names=["system.ai.slack"])
    remove = Mock()
    monkeypatch.setattr(mdm, "remove", remove)
    monkeypatch.setattr(cli, "revert", Mock(side_effect=AssertionError("normal revert")))
    assert CliRunner().invoke(cli.app, ["revert", "--mdm-desktop"]).exit_code == 0
    remove.assert_called_once()
