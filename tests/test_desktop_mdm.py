"""MDM filesystem lifecycle with external workspace and machine ownership boundaries replaced."""

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

from tests.test_desktop_bundle import system_source as system_source
from ucode import cli, config_io, managed_config, mcp, skills_download, workspace
from ucode.agents.desktop import claude_config, cowork, mdm, setup
from ucode.databricks import AnthropicModelCatalog
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
    shutil.rmtree(tmp_path / "Native")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(mdm, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(mdm, "MDM_ROOT", tmp_path / "machine/mdm")
    monkeypatch.setattr(mdm, "POLICY_PATH", tmp_path / "machine/policy.plist")
    # These temporary directories belong to the test runner, not root. Validate ownership separately.
    monkeypatch.setattr(mdm, "_check_machine_path", lambda *a, **kw: None)
    monkeypatch.setattr(mdm, "_runtime", lambda: dict(RUNTIME))
    monkeypatch.setattr(mdm, "validate_cli_discovery", lambda: None)
    state = SimpleNamespace(
        admin=True,
        source=system_source,
        home=home,
        manifest={
            "enabled_agents": [
                {"agent": "CODING_AGENT_CLAUDE_CODE", "config": {"tracing": {"enabled": True}}}
            ],
            "mcp_servers": {"names": ["system.ai.slack"]},
            "skills": {"names": [SKILL.fqn]},
        },
        skill=b"---\nname: review\ndescription: Review code\n---\nCheck tests.\n",
    )
    monkeypatch.setattr(mdm.system_cross_os, "is_admin", lambda: state.admin)
    state.auth = Mock(return_value="provisioning-secret")
    monkeypatch.setattr(mdm.databricks, "get_databricks_token", state.auth)
    monkeypatch.setattr(
        managed_config, "fetch_managed_coding_agent_configs", lambda *a: ([state.manifest], None)
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


def _snapshot(device):
    return {
        "source": cowork._marketplace_files(device.source),
        "mdm": cowork._marketplace_files(mdm.MDM_ROOT),
        "policy": mdm.POLICY_PATH.read_bytes(),
    }


def test_provision_without_account_has_no_admin_credentials_or_user_writes(device, tmp_path):
    shutil.rmtree(tmp_path / "Desktop")
    mdm.configure(WORKSPACE, "ADMIN_PROFILE")
    policy = plistlib.loads(mdm.POLICY_PATH.read_bytes())
    assert all(isinstance(value, str) for value in policy.values())
    assert policy["inferenceGatewayBaseUrl"] == f"{WORKSPACE}/ai-gateway/anthropic"
    assert json.loads(policy["inferenceModels"]) == [{"name": "system.ai.claude-sonnet-4-6"}]
    assert policy["modelDiscoveryEnabled"] == "false"
    assert policy["toolSearchEnabled"] == "true"
    assert policy["otlpTracesEnabled"] == "true"
    assert policy["inferenceCredentialHelperTimeoutSec"] == "300"
    helper_args = json.loads(policy["inferenceCredentialHelperArgs"])
    assert helper_args[:4] == ["claude-desktop-auth", "--host", WORKSPACE, "--profile"]
    assert helper_args[4].startswith("UG_DESKTOP_")
    servers = json.loads(policy["managedMcpServers"])
    assert [entry["name"] for entry in servers] == ["slack"]
    assert servers[0]["command"] == str(mdm.MDM_ROOT / "ug")
    assert servers[0]["transport"] == "stdio"
    assert "type" not in servers[0]
    assert servers[0]["args"][:1] == ["mcp-proxy"]
    assert servers[0]["args"][-2:] == ["--profile", helper_args[4]]
    plugin = device.source
    assert not (plugin / ".mcp.json").exists()
    manifest = json.loads((plugin / ".claude-plugin/plugin.json").read_bytes())
    assert manifest["installationPreference"] == "required"
    assert "mcpServers" not in manifest
    assert json.loads((plugin / "version.json").read_bytes()) == {"version": manifest["version"]}
    assert (plugin / "skills/review/SKILL.md").read_bytes() == device.skill
    assert (plugin / "skills/review/asset.bin").read_bytes() == b"\x00\xff"
    assert (plugin / "skills/unity-gateway-connectors/SKILL.md").is_file()
    hooks = json.loads((plugin / "hooks/hooks.json").read_bytes())["hooks"]
    assert set(hooks) == {"SessionStart", "UserPromptSubmit"}
    for handlers in hooks.values():
        command = shlex.split(handlers[0]["hooks"][0]["command"])
        assert command[:2] == [str(mdm.MDM_ROOT / "ug"), "cowork-hook"]
        assert command[command.index("--profile") + 1] == helper_args[4]
    for bundle in (cowork._marketplace_files(plugin), cowork._marketplace_files(mdm.MDM_ROOT)):
        for data, _ in bundle.values():
            assert b"provisioning-secret" not in data
            assert b"ADMIN_PROFILE" not in data
            assert str(device.home).encode() not in data
            assert b"desktop-mdm-sync" not in data
    assert not list(device.home.iterdir())
    assert not cowork._marketplace_root().exists()
    assert not cowork._state_path().exists()
    assert not (mdm.MDM_ROOT / "deployment.json").exists()
    if os.name != "nt":
        assert (mdm.MDM_ROOT / "ug").stat().st_mode & 0o777 == 0o755
    device.auth.assert_called_once_with(WORKSPACE, "ADMIN_PROFILE", browser_login=False)


def test_provision_update_and_remove_root_files(device):
    mdm.configure(WORKSPACE, "admin")
    plugin = device.source
    before = json.loads((plugin / "version.json").read_bytes())
    device.skill = device.skill.replace(b"Check tests.", b"Updated verification code.")
    device.manifest["mcp_servers"]["names"].append("system.ai.github")
    device.manifest["enabled_agents"][0]["config"]["tracing"]["enabled"] = False
    mdm.configure(WORKSPACE, "admin")
    policy = plistlib.loads(mdm.POLICY_PATH.read_bytes())
    assert "otlpEndpoint" not in policy
    assert not (mdm.MDM_ROOT / "otel-headers.sh").exists()
    assert (plugin / "skills/review/SKILL.md").read_bytes() == device.skill
    assert json.loads((plugin / "version.json").read_bytes()) != before
    assert len(json.loads(policy["managedMcpServers"])) == 2
    assert not (plugin / ".mcp.json").exists()
    device.auth.reset_mock()
    mdm.remove()
    assert not device.source.exists() and not mdm.POLICY_PATH.exists()
    assert not mdm.MDM_ROOT.exists()
    mdm.remove()
    device.auth.assert_not_called()


def test_provision_accepts_agent_name_from_api(device):
    device.manifest["enabled_agents"][0]["agent"] = "claude_code"
    device.manifest["enabled_agents"][0]["config"]["http_headers"] = {"x-org-header": "value"}
    mdm.configure(WORKSPACE, "admin")
    policy = plistlib.loads(mdm.POLICY_PATH.read_bytes())
    assert json.loads(policy["inferenceCustomHeaders"])["x-org-header"] == "value"
    assert policy["otlpTracesEnabled"] == "true"


def test_provision_rejects_workspace_without_claude(device):
    device.manifest["enabled_agents"] = [{"agent": "CODING_AGENT_CODEX", "config": {}}]
    with pytest.raises(RuntimeError, match="does not enable Claude"):
        mdm.configure(WORKSPACE, "admin")
    assert not mdm.POLICY_PATH.exists()
    assert not device.source.exists()


def test_missing_provisioning_auth_fails_before_machine_writes(device):
    device.auth.side_effect = RuntimeError("OAuth session expired")
    with pytest.raises(RuntimeError, match="authenticate before.*OAuth session expired"):
        mdm.configure(WORKSPACE, "ADMIN_PROFILE")
    device.auth.assert_called_once_with(WORKSPACE, "ADMIN_PROFILE", browser_login=False)
    assert not mdm.MDM_ROOT.exists()
    assert not device.source.exists()
    assert not mdm.POLICY_PATH.exists()


@pytest.mark.parametrize("workspace_key", [WORKSPACE, WORKSPACE + "?w=111"])
def test_provision_rejects_shared_host_selection_before_auth(device, workspace_key):
    workspace.activate(WORKSPACE, "111")
    try:
        with pytest.raises(RuntimeError, match="dedicated workspace hostname"):
            mdm.configure(workspace_key, "ADMIN_PROFILE")
    finally:
        workspace.activate(None, None)
    device.auth.assert_not_called()
    assert not mdm.MDM_ROOT.exists()
    assert not device.source.exists()


@pytest.mark.parametrize("reason", [None, "FEATURE_DISABLED: coding agent configs unavailable"])
def test_provision_without_managed_config(device, monkeypatch, reason):
    monkeypatch.setattr(
        managed_config, "fetch_managed_coding_agent_configs", lambda *a: ([], reason)
    )
    mdm.configure(WORKSPACE, "admin")
    policy = plistlib.loads(mdm.POLICY_PATH.read_bytes())
    assert json.loads(policy["inferenceModels"]) == [{"name": "system.ai.claude-sonnet-4-6"}]
    assert not (device.source / ".mcp.json").exists()


def test_provision_with_unrestricted_agents_keeps_managed_connectors_and_skills(device):
    device.manifest["enabled_agents"] = []
    mdm.configure(WORKSPACE, "ADMIN_PROFILE")
    plugin = device.source
    assert (plugin / "skills/review/SKILL.md").read_bytes() == device.skill
    assert [
        entry["name"]
        for entry in json.loads(plistlib.loads(mdm.POLICY_PATH.read_bytes())["managedMcpServers"])
    ] == ["slack"]


def test_provision_leaves_existing_user_registration_unchanged(device):
    user_source = cowork._marketplace_root()
    cowork._write_marketplace(user_source, user_source / "plugins/ug", [], [])
    root = cowork.discover_cowork_root()
    cowork._install_plugin(root, user_source)
    registry_path = root / "cowork_plugins/installed_plugins.json"
    registry = config_io.read_json_safe(registry_path)
    registry["plugins"]["personal@local"] = [{"installPath": "/personal/plugin"}]
    config_io.atomic_write_json(registry_path, registry)
    before = cowork._marketplace_files(root)
    user_before = cowork._marketplace_files(user_source)
    mdm.configure(WORKSPACE, "admin")
    assert cowork._marketplace_files(root) == before
    assert cowork._marketplace_files(user_source) == user_before
    mdm.remove()
    assert cowork._marketplace_files(root) == before
    assert cowork._marketplace_files(user_source) == user_before


@pytest.mark.parametrize("failure", ["fetch", "malformed_mcp", "missing_skill", "download"])
def test_failed_discovery_or_download_preserves_previous_deployment(device, monkeypatch, failure):
    mdm.configure(WORKSPACE, "admin")
    before = _snapshot(device)
    device.skill += b"New version.\n"
    if failure == "fetch":
        monkeypatch.setattr(
            managed_config, "fetch_managed_coding_agent_configs", lambda *a: (None, "denied")
        )
    elif failure == "malformed_mcp":
        device.manifest["mcp_servers"]["names"].append("bad-name")
    elif failure == "missing_skill":
        monkeypatch.setattr(skills_download, "get_skill", lambda *a: None)
    else:
        monkeypatch.setattr(skills_download, "fetch_skill_bundle", lambda *a: (None, "unavailable"))
    with pytest.raises(RuntimeError):
        mdm.configure(WORKSPACE, "admin")
    assert _snapshot(device) == before


def test_failed_publication_is_reported_and_reconfigure_repairs_it(device, monkeypatch):
    mdm.configure(WORKSPACE, "admin")
    policy_before = mdm.POLICY_PATH.read_bytes()
    device.skill += b"New version.\n"
    device.manifest["enabled_agents"][0]["config"]["tracing"]["enabled"] = False
    write = claude_config._write_or_remove

    def fail_policy(path, contents, *, mode):
        if path == mdm.POLICY_PATH:
            raise OSError("test publication failure")
        return write(path, contents, mode=mode)

    monkeypatch.setattr(claude_config, "_write_or_remove", fail_policy)
    with pytest.raises(RuntimeError, match="test publication failure"):
        mdm.configure(WORKSPACE, "admin")
    assert mdm.POLICY_PATH.read_bytes() == policy_before
    assert (device.source / "skills/review/SKILL.md").read_bytes() == device.skill

    monkeypatch.setattr(claude_config, "_write_or_remove", write)
    mdm.configure(WORKSPACE, "admin")
    assert "otlpEndpoint" not in plistlib.loads(mdm.POLICY_PATH.read_bytes())
    assert not (mdm.MDM_ROOT / "otel-headers.sh").exists()


@pytest.mark.parametrize("target", ["policy", "source"])
def test_existing_policy_is_preserved_and_generated_source_is_replaced(device, target):
    path = mdm.POLICY_PATH if target == "policy" else device.source / "foreign.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o755)
    path.write_bytes(
        plistlib.dumps({"inferenceCredentialHelper": "/other/helper"})
        if target == "policy"
        else b"IT-owned"
    )
    if target == "policy":
        with pytest.raises(RuntimeError, match="not this"):
            mdm.configure(WORKSPACE, "admin")
        assert plistlib.loads(path.read_bytes()) == {"inferenceCredentialHelper": "/other/helper"}
    else:
        path.chmod(0o644)
        mdm.configure(WORKSPACE, "admin")
        assert not path.exists()
        assert (device.source / ".claude-plugin/plugin.json").is_file()


@pytest.mark.parametrize(
    "plugins", [None, [], {cowork.PLUGIN_ID: [None]}, {cowork.PLUGIN_ID: [{"installPath": 123}]}]
)
def test_provision_does_not_inspect_or_repair_user_plugin_registration(device, plugins):
    root = cowork.discover_cowork_root()
    registry = root / "cowork_plugins/installed_plugins.json"
    config_io.atomic_write_json(registry, {"plugins": plugins})
    before = registry.read_bytes()
    mdm.configure(WORKSPACE, "admin")
    assert registry.read_bytes() == before


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
    device.manifest["skills"] = {}
    device.manifest["mcp_servers"] = {}
    mdm.configure(WORKSPACE, "admin")
    plugin = device.source
    assert not (plugin / "skills/review").exists()
    assert not (plugin / "hooks/hooks.json").exists()
    assert not (plugin / ".mcp.json").exists()
    assert json.loads(plistlib.loads(mdm.POLICY_PATH.read_bytes())["managedMcpServers"]) == []


def test_provision_does_not_run_native_plugin_commands(device, monkeypatch):
    native_commands = Mock(side_effect=AssertionError("MDM must not register per-user plugins"))
    monkeypatch.setattr(cowork, "_run_claude", native_commands)
    monkeypatch.setattr(cowork, "_run_claude_json", native_commands)
    mdm.configure(WORKSPACE, "admin")
    mdm.remove()
    native_commands.assert_not_called()


def test_reconfigure_replaces_edited_generated_policy(device):
    mdm.configure(WORKSPACE, "admin")
    original = mdm.POLICY_PATH.read_bytes()
    edited = plistlib.loads(original)
    edited["inferenceGatewayBaseUrl"] = "https://other.example"
    mdm.POLICY_PATH.write_bytes(plistlib.dumps(edited))
    mdm.configure(WORKSPACE, "admin")
    assert mdm.POLICY_PATH.read_bytes() == original


def test_dry_run_has_no_auth_discovery_or_writes(device):
    config_io.set_dry_run(True)
    mdm.configure(WORKSPACE, "admin")
    device.auth.assert_not_called()
    assert not device.source.exists() and not mdm.MDM_ROOT.exists()


@pytest.mark.parametrize("platform,admin", [("linux", True), ("win32", True), ("darwin", False)])
def test_platform_and_identity_guards(monkeypatch, platform, admin):
    monkeypatch.setattr(mdm, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(mdm.system_cross_os, "is_admin", lambda: admin)
    with pytest.raises(RuntimeError):
        mdm.validate_mdm_execution()


@pytest.mark.parametrize("private", [False, True])
def test_shared_runtime_checks_access_but_retains_virtualenv_path(monkeypatch, tmp_path, private):
    executable = tmp_path / "venv/bin/python"
    resolved = tmp_path / "system/python"
    monkeypatch.setattr(Path, "exists", lambda p: True)
    monkeypatch.setattr(Path, "is_file", lambda p: True)
    monkeypatch.setattr(Path, "is_dir", lambda p: False)
    monkeypatch.setattr(Path, "is_symlink", lambda p: False)
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


@pytest.mark.skipif(os.name == "nt", reason="macOS runtime validation uses POSIX symlinks")
@pytest.mark.parametrize("problem", [None, "owner", "writable", "private"])
def test_shared_runtime_checks_intermediate_link_parents(monkeypatch, tmp_path, problem):
    executable = tmp_path / "venv/bin/python"
    alias = tmp_path / "alias/python"
    target = tmp_path / "runtime/python"
    for entry in (executable, alias, target):
        entry.parent.mkdir(parents=True)
    target.write_text("runtime sentinel; never executed")
    target.chmod(0o755)
    executable.symlink_to(alias)
    alias.symlink_to(target)
    original_stat = Path.stat

    def machine_stat(path, *args, **kwargs):
        info = list(original_stat(path, *args, **kwargs))
        info[0] &= ~0o022
        if stat.S_ISDIR(info[0]):
            info[0] |= 0o005
        info[4] = 0
        if path == alias.parent:
            if problem == "owner":
                info[4] = 501
            elif problem == "writable":
                info[0] |= 0o020
            elif problem == "private":
                info[0] &= ~0o007
        return os.stat_result(info)

    monkeypatch.setattr(Path, "stat", machine_stat)
    if problem is None:
        assert mdm._shared_path(str(executable), "Python", executable=True) == executable
    else:
        with pytest.raises(
            RuntimeError, match="accessible" if problem == "private" else "root-owned"
        ):
            mdm._shared_path(str(executable), "Python", executable=True)


def test_runtime_probe_reads_installed_package_path(monkeypatch):
    # Exercise the real isolated interpreter without requiring root-owned test files or CLIs.
    monkeypatch.setattr(mdm, "_shared_path", lambda value, *a, **kw: Path(value))
    monkeypatch.setattr(mdm, "shutil", SimpleNamespace(which=lambda name: RUNTIME[name]))
    monkeypatch.setattr(mdm.databricks, "databricks_cli_path", lambda: RUNTIME["databricks"])

    expected = {name: str(Path(value)) for name, value in RUNTIME.items()}
    assert mdm._runtime() == {**expected, "python": os.path.abspath(sys.executable)}


def test_runtime_rejects_untrusted_cli_before_version_discovery(monkeypatch, tmp_path):
    candidate = tmp_path / "databricks"
    candidate.write_text("untrusted test file")
    candidate.chmod(0o777)
    shared_path = mdm._shared_path

    def validate(value, name, **kwargs):
        if value == str(candidate):
            return shared_path(value, name, **kwargs)
        return Path(value)

    monkeypatch.setattr(mdm, "_shared_path", validate)
    monkeypatch.setattr(mdm.databricks, "_iter_databricks_executables", lambda: [str(candidate)])
    discover = Mock(side_effect=AssertionError("Untrusted CLI must not be probed"))
    subprocess_run = Mock(side_effect=AssertionError("Untrusted CLI must not be executed"))
    monkeypatch.setattr(mdm.databricks, "databricks_cli_path", discover)
    monkeypatch.setattr(mdm.subprocess_cross_os, "run", subprocess_run)
    with pytest.raises(RuntimeError, match="root-owned"):
        mdm._runtime()
    discover.assert_not_called()
    subprocess_run.assert_not_called()


@pytest.mark.parametrize(
    ("flags", "profile"),
    [
        (["--global"], "CACHED"),
        (["--global"], ""),
        (["--global", "--dry-run"], None),
    ],
)
@pytest.mark.parametrize("cli_name", ["Databricks CLI", "Claude CLI"])
def test_profile_lookup_rejects_untrusted_cli_before_execution(
    monkeypatch, tmp_path, flags, profile, cli_name
):
    command = "databricks" if cli_name == "Databricks CLI" else "claude"
    candidate = tmp_path / (f"{command}.exe" if os.name == "nt" else command)
    candidate.write_text("untrusted test file")
    candidate.chmod(0o777)
    shared_path = mdm._shared_path

    def validate(value, name, **kwargs):
        if name == cli_name:
            return shared_path(value, name, **kwargs)
        return Path(value)

    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(mdm.system_cross_os, "is_admin", lambda: True)
    monkeypatch.setattr(mdm, "_shared_path", validate)
    if cli_name == "Claude CLI":
        monkeypatch.setattr(mdm.shutil, "which", lambda name: str(candidate))
    forbidden = Mock(side_effect=AssertionError("Untrusted CLI must not be executed"))
    monkeypatch.setattr(mdm.databricks, "run", forbidden)
    monkeypatch.setattr(mdm.subprocess_cross_os, "run", forbidden)
    monkeypatch.setattr(cli, "install_databricks_cli", forbidden)
    monkeypatch.setattr(cli, "install_tool_binary", forbidden)
    target = (
        ["--workspace", "https://workspace.example.com"]
        if profile is None
        else ["--profile", profile]
    )
    result = CliRunner().invoke(cli.app, ["configure", "--agents", "claude", *target, *flags])
    assert result.exit_code == 1, result.output
    assert "root-owned" in " ".join(result.output.split())
    forbidden.assert_not_called()


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


def test_global_with_cached_oauth_does_not_force_browser_login(device, monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.delenv("DATABRICKS_BEARER", raising=False)
    monkeypatch.delenv("DATABRICKS_BEARER_COMMAND", raising=False)
    monkeypatch.setattr(cli, "_parse_profile_option", lambda value: [(WORKSPACE, value)])
    events = []
    monkeypatch.setattr(mdm, "validate_cli_discovery", lambda: events.append("validated"))
    monkeypatch.setattr(cli, "install_databricks_cli", lambda: events.append("installed"))
    login = Mock(side_effect=AssertionError("MDM must not start browser login"))
    monkeypatch.setattr(cli, "run_databricks_login", login)
    monkeypatch.setattr(cli, "ensure_databricks_auth", login)
    token = Mock(side_effect=["cached-oauth", RuntimeError("stop before gateway discovery")])
    monkeypatch.setattr(cli, "get_databricks_token", token)
    result = CliRunner().invoke(
        cli.app, ["configure", "--global", "--agents", "claude", "--profile", "CACHED"]
    )
    assert result.exit_code == 1
    assert "stop before gateway discovery" in result.output
    assert events == ["validated", "installed"]
    assert token.call_args_list[0].args == (WORKSPACE, "CACHED")
    assert token.call_args_list[0].kwargs == {"browser_login": False}
    assert token.call_count == 2
    login.assert_not_called()


def test_cli_removes_mdm_machine_files(device, monkeypatch):
    remove = Mock()
    monkeypatch.setattr(mdm, "remove", remove)
    monkeypatch.setattr(cli, "revert", Mock(side_effect=AssertionError("normal revert")))
    assert CliRunner().invoke(cli.app, ["revert", "--mdm-desktop"]).exit_code == 0
    remove.assert_called_once()
