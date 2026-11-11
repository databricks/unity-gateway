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
from ucode import cli, config_io, managed_config, managed_files, mcp, skills_download, workspace
from ucode.agents.desktop import cowork, mdm, setup
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
    assert json.loads(policy["inferenceModels"]) == [
        {"name": "system.ai.claude-sonnet-4-6", "labelOverride": "Claude Sonnet 4.6"}
    ]
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
        assert (mdm.MDM_ROOT / "otel-headers.sh").stat().st_mode & 0o777 == 0o755
        assert mdm.POLICY_PATH.stat().st_mode & 0o777 == 0o644
    device.auth.assert_called_once_with(WORKSPACE, "ADMIN_PROFILE")


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
    with pytest.raises(RuntimeError, match="OAuth session expired"):
        mdm.configure(WORKSPACE, "ADMIN_PROFILE")
    device.auth.assert_called_once_with(WORKSPACE, "ADMIN_PROFILE")
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
    assert json.loads(policy["inferenceModels"]) == [
        {"name": "system.ai.claude-sonnet-4-6", "labelOverride": "Claude Sonnet 4.6"}
    ]
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


@pytest.mark.parametrize(
    "error",
    [
        OSError("test publication failure"),
        subprocess.CalledProcessError(1, "privileged-writer", stderr="test publication failure"),
    ],
)
def test_failed_publication_is_reported_and_reconfigure_repairs_it(device, monkeypatch, error):
    mdm.configure(WORKSPACE, "admin")
    policy_before = mdm.POLICY_PATH.read_bytes()
    device.skill += b"New version.\n"
    device.manifest["enabled_agents"][0]["config"]["tracing"]["enabled"] = False
    write = managed_files._sudo_replace

    def fail_policy(path, contents):
        if path == mdm.POLICY_PATH:
            raise error
        return write(path, contents)

    monkeypatch.setattr(managed_files, "_sudo_replace", fail_policy)
    with pytest.raises(RuntimeError, match="test publication failure"):
        mdm.configure(WORKSPACE, "admin")
    assert mdm.POLICY_PATH.read_bytes() == policy_before
    assert (device.source / "skills/review/SKILL.md").read_bytes() == device.skill

    monkeypatch.setattr(managed_files, "_sudo_replace", write)
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


def test_dry_run_has_no_auth_discovery_or_writes(device, monkeypatch):
    monkeypatch.setattr(config_io, "_dry_run", True)
    mdm.configure(WORKSPACE, "admin")
    device.auth.assert_not_called()
    assert not device.source.exists() and not mdm.MDM_ROOT.exists()


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_platform_guard(monkeypatch, platform):
    monkeypatch.setattr(mdm, "sys", SimpleNamespace(platform=platform))
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
    monkeypatch.setattr(Path, "lstat", lambda path: machine_stat(path, follow_symlinks=False))
    if problem is None:
        assert mdm._shared_path(str(executable), "Python", executable=True) == executable
    else:
        with pytest.raises(
            RuntimeError, match="accessible" if problem == "private" else "root-owned"
        ):
            mdm._shared_path(str(executable), "Python", executable=True)


@pytest.mark.skipif(os.name == "nt", reason="macOS runtime validation uses POSIX symlinks")
@pytest.mark.parametrize("version_link_owner", [0, 501])
def test_shared_runtime_accepts_root_owned_uv_python_version_link(
    monkeypatch, tmp_path, version_link_owner
):
    install = tmp_path / "unity-gateway/python"
    runtime = install / "cpython-3.12.12-macos-aarch64-none/bin/python3.12"
    version_link = install / "cpython-3.12-macos-aarch64-none"
    executable = tmp_path / "unity-gateway/tools/unity-gateway/bin/python"
    for entry in (runtime, executable):
        entry.parent.mkdir(parents=True)
    runtime.write_text("runtime sentinel; never executed")
    runtime.chmod(0o755)
    version_link.symlink_to(runtime.parents[1].name)
    executable.symlink_to(version_link / "bin/python3.12")
    original_stat = Path.stat

    def machine_stat(path, *args, follow_symlinks=True, **kwargs):
        info = list(original_stat(path, *args, follow_symlinks=follow_symlinks, **kwargs))
        info[0] &= ~0o022
        if stat.S_ISDIR(info[0]):
            info[0] |= 0o005
        info[4] = version_link_owner if path == version_link and not follow_symlinks else 0
        return os.stat_result(info)

    monkeypatch.setattr(Path, "stat", machine_stat)
    monkeypatch.setattr(Path, "lstat", lambda path: machine_stat(path, follow_symlinks=False))
    if version_link_owner == 0:
        assert mdm._shared_path(str(executable), "Python", executable=True) == executable
    else:
        with pytest.raises(RuntimeError, match="symlink must be root-owned"):
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


@pytest.mark.skipif(os.name == "nt", reason="Windows files report uid 0, so ownership never fails")
@pytest.mark.parametrize("cli_name", ["Databricks CLI", "Claude CLI"])
def test_mdm_rejects_untrusted_cli_before_auth_or_publication(monkeypatch, tmp_path, cli_name):
    command = "databricks" if cli_name == "Databricks CLI" else "claude"
    candidate = tmp_path / command
    candidate.write_text("untrusted test file")
    candidate.chmod(0o777)
    shared_path = mdm._shared_path

    def validate(value, name, **kwargs):
        if name == cli_name:
            return shared_path(value, name, **kwargs)
        return Path(value)

    monkeypatch.setattr(mdm, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(mdm, "_shared_path", validate)
    if cli_name == "Claude CLI":
        monkeypatch.setattr(mdm.shutil, "which", lambda name: str(candidate))
    forbidden = Mock(side_effect=AssertionError("Invalid MDM runtime must not be used"))
    monkeypatch.setattr(mdm.databricks, "get_databricks_token", forbidden)
    monkeypatch.setattr(mdm.subprocess_cross_os, "run", forbidden)
    monkeypatch.setattr(mdm, "_publish", forbidden)
    with pytest.raises(RuntimeError, match="root-owned"):
        mdm.configure(WORKSPACE, "CACHED")
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


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize("external_auth", [None, "DATABRICKS_BEARER", "DATABRICKS_BEARER_COMMAND"])
def test_global_preserves_existing_auth_flow(device, monkeypatch, agent, external_auth):
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.delenv("DATABRICKS_BEARER", raising=False)
    monkeypatch.delenv("DATABRICKS_BEARER_COMMAND", raising=False)
    if external_auth:
        monkeypatch.setenv(external_auth, "external-credential-input")
    monkeypatch.setattr(cli, "_parse_profile_option", lambda value: [(WORKSPACE, value)])
    monkeypatch.setattr(cli, "install_databricks_cli", lambda: None)
    login = Mock()
    ensure_auth = Mock()
    monkeypatch.setattr(cli, "run_databricks_login", login)
    monkeypatch.setattr(cli, "ensure_databricks_auth", ensure_auth)
    # Stop after authentication routing; no gateway access or agent configuration is needed.
    token = Mock(side_effect=RuntimeError("stop before gateway discovery"))
    monkeypatch.setattr(cli, "get_databricks_token", token)
    result = CliRunner().invoke(
        cli.app, ["configure", "--global", "--agents", agent, "--profile", "PROVISIONING"]
    )
    assert result.exit_code == 1
    assert "stop before gateway discovery" in result.output
    token.assert_called_once_with(WORKSPACE, "PROVISIONING")
    if external_auth:
        ensure_auth.assert_called_once_with(WORKSPACE, "PROVISIONING")
        login.assert_not_called()
    else:
        login.assert_called_once_with(WORKSPACE, "PROVISIONING")
        ensure_auth.assert_not_called()


@pytest.mark.parametrize("flag", ["--global", "--user", "--mdm", "--mdm-desktop"])
def test_cli_revert_has_no_scope_flags(device, monkeypatch, flag):
    remove = Mock()
    monkeypatch.setattr(mdm, "remove", remove)
    revert = Mock()
    monkeypatch.setattr(cli, "revert", revert)
    result = CliRunner().invoke(cli.app, ["revert", flag])
    assert result.exit_code == 2
    remove.assert_not_called()
    revert.assert_not_called()


def test_mdm_removal_without_deployment_never_requests_elevation(device, monkeypatch):
    command = Mock(side_effect=AssertionError("Nothing to remove"))
    monkeypatch.setattr(managed_files, "_sudo_replace_command", command)
    assert mdm.remove() is False
    command.assert_not_called()


@pytest.mark.parametrize("deployed", [False, True])
def test_remove_preserves_foreign_policy_without_blocking_ug_cleanup(device, deployed):
    if deployed:
        mdm.configure(WORKSPACE, "admin")
    mdm.POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
    foreign = plistlib.dumps({"inferenceCredentialHelper": "/other/helper"})
    mdm.POLICY_PATH.write_bytes(foreign)
    assert mdm.remove() is deployed
    assert mdm.POLICY_PATH.read_bytes() == foreign
    assert not device.source.exists()
    assert not mdm.MDM_ROOT.exists()
    assert mdm.remove() is False


@pytest.mark.parametrize("operation", ["configure", "remove"])
def test_owned_binary_policy_can_be_updated_or_removed(device, operation):
    mdm.configure(WORKSPACE, "admin")
    policy = plistlib.loads(mdm.POLICY_PATH.read_bytes())
    mdm.POLICY_PATH.write_bytes(plistlib.dumps(policy, fmt=plistlib.FMT_BINARY))
    if operation == "configure":
        mdm.configure(WORKSPACE, "admin")
        assert plistlib.loads(mdm.POLICY_PATH.read_bytes()) == policy
        assert device.source.exists()
    else:
        assert mdm.remove() is True
        assert not mdm.POLICY_PATH.exists()
        assert not device.source.exists()
        assert not mdm.MDM_ROOT.exists()


def test_remove_without_sudo_raises_write_unavailable(device, monkeypatch):
    mdm.configure(WORKSPACE, "admin")
    sudo_refused = subprocess.CalledProcessError(1, ["sudo", "-n"], stderr="a password is required")
    monkeypatch.setattr(managed_files, "reconcile_privileged_files", Mock(side_effect=sudo_refused))
    with pytest.raises(managed_files.ManagedFileWriteUnavailable, match="password is required"):
        mdm.remove()


def test_revert_skips_mdm_when_privilege_is_unavailable(monkeypatch, capsys):
    monkeypatch.setattr(setup.sys, "platform", "darwin")
    unavailable = managed_files.ManagedFileWriteUnavailable("sudo refused")
    monkeypatch.setattr(mdm, "remove", Mock(side_effect=unavailable))
    assert setup.revert_claude_desktop_mdm() == "skipped"
    assert "an administrator must run" in capsys.readouterr().out


def test_revert_fails_on_a_genuine_mdm_error(monkeypatch):
    monkeypatch.setattr(setup.sys, "platform", "darwin")
    monkeypatch.setattr(mdm, "remove", Mock(side_effect=RuntimeError("unsafe path")))
    assert setup.revert_claude_desktop_mdm() == "failed"


def test_corrupt_policy_does_not_block_revert_and_is_left_alone(device):
    mdm.configure(WORKSPACE, "admin")
    mdm.POLICY_PATH.write_bytes(b"not a plist")
    assert mdm.remove() is True
    assert mdm.POLICY_PATH.read_bytes() == b"not a plist"
    assert not device.source.exists()
    assert not (mdm.MDM_ROOT / "ug").exists()


def test_configure_refuses_to_overwrite_a_corrupt_policy(device):
    mdm.POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
    mdm.POLICY_PATH.write_bytes(b"not a plist")
    with pytest.raises(RuntimeError, match="not this UG deployment"):
        mdm.configure(WORKSPACE, "admin")
    assert mdm.POLICY_PATH.read_bytes() == b"not a plist"
