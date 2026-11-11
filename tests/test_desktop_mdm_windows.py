"""Windows publication with real temporary files and simulated OS registry/ACL boundaries."""

import json
import os
import runpy
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ucode import managed_config, managed_files
from ucode.agents.desktop import mdm, mdm_windows, setup
from ucode.databricks import AnthropicModelCatalog
from ucode.os_compatibility import windows_machine_paths

WINDOWS_REPLACE = managed_files._windows_replace


class Registry:
    HKEY_LOCAL_MACHINE = "HKLM"
    KEY_READ = 1
    KEY_SET_VALUE = 2
    KEY_WOW64_64KEY = 256

    def __init__(self):
        self.values = {"unrelatedControl": (7, 4)}
        self.access = []
        self.fail_write = False

    def OpenKey(self, hive, path, reserved, access):
        assert hive == self.HKEY_LOCAL_MACHINE and path == mdm_windows.POLICY_KEY
        self.access.append(access)
        return nullcontext(self)

    CreateKeyEx = OpenKey

    def QueryInfoKey(self, key):
        return 1, len(self.values), 0

    def EnumValue(self, key, index):
        name = list(self.values)[index]
        return name, *self.values[name]

    def SetValueEx(self, key, name, reserved, kind, value):
        if self.fail_write:
            self.fail_write = False
            raise PermissionError("registry write denied")
        existing = next((k for k in self.values if k.casefold() == name.casefold()), name)
        self.values[existing] = value, kind

    def DeleteValue(self, key, name):
        existing = next((k for k in self.values if k.casefold() == name.casefold()), None)
        if existing is None:
            raise FileNotFoundError(name)
        del self.values[existing]


@pytest.fixture
def machine(tmp_path, monkeypatch):
    root = tmp_path / "Program Files"
    root.mkdir()
    monkeypatch.setattr(managed_files, "current_os", lambda: managed_files.OS.WINDOWS)
    monkeypatch.setattr(managed_files.system_cross_os, "windows_program_files_dir", lambda: root)
    monkeypatch.setattr(managed_files.system_cross_os, "is_admin", lambda: True)
    monkeypatch.setattr(
        managed_files, "claude_desktop_org_plugin_path", lambda: root / "Claude/org-plugins/ug"
    )
    monkeypatch.setattr(managed_files, "_windows_replace", WINDOWS_REPLACE)
    # Linux has no Windows DACLs or registry. Keep publication, rollback and ownership logic real.
    monkeypatch.setattr(windows_machine_paths, "validate", lambda path, **kw: None)
    registry = Registry()
    monkeypatch.setattr(mdm_windows, "_registry", lambda: registry)
    source = tmp_path / "generated-plugin"
    (source / ".claude-plugin").mkdir(parents=True)
    (source / ".claude-plugin/plugin.json").write_text('{"name":"ug","version":"1"}')
    runtime = managed_files.windows_desktop_mdm_root()
    return SimpleNamespace(
        root=root,
        registry=registry,
        source=source,
        runtime=runtime,
        plugin=managed_files.claude_desktop_org_plugin_path(),
        files={runtime / "runtime.py": b"runtime", runtime / "otel-headers.cmd": None},
        policy={"inferenceProvider": "gateway", "inferenceCredentialHelper": "shared-python"},
    )


def test_publish_refresh_remove_preserves_unrelated_policy_and_files(machine):
    mdm_windows.publish(machine.source, machine.files, machine.policy)
    assert machine.registry.values["inferenceProvider"] == ("gateway", 1)
    assert all(access & Registry.KEY_WOW64_64KEY for access in machine.registry.access)
    assert (machine.runtime / "runtime.py").read_bytes() == b"runtime"
    assert (machine.plugin / ".claude-plugin/plugin.json").exists()
    unrelated = machine.runtime / "administrator.txt"
    unrelated.write_text("keep")

    policy = {**machine.policy, "otlpTracesEnabled": "true"}
    mdm_windows.publish(machine.source, machine.files, policy)
    mdm_windows.publish(machine.source, machine.files, machine.policy)
    assert "otlpTracesEnabled" not in machine.registry.values
    assert mdm_windows.remove()
    assert machine.registry.values == {"unrelatedControl": (7, 4)}
    assert unrelated.read_text() == "keep"
    assert not machine.plugin.exists()
    assert not mdm_windows.remove()


@pytest.mark.parametrize("field", ["inferenceProvider", "INFERENCECREDENTIALHELPER"])
def test_foreign_policy_is_rejected_before_publication(machine, field):
    machine.registry.values[field] = ("foreign", 1)
    before = dict(machine.registry.values)
    with pytest.raises(RuntimeError, match="another deployment"):
        mdm_windows.publish(machine.source, machine.files, machine.policy)
    assert machine.registry.values == before
    assert not machine.runtime.exists()
    assert not machine.plugin.exists()


@pytest.mark.parametrize("operation", ["publish", "remove"])
def test_external_policy_edits_are_preserved(machine, operation):
    mdm_windows.publish(machine.source, machine.files, machine.policy)
    machine.registry.values["inferenceProvider"] = ("foreign", 1)
    with pytest.raises(RuntimeError, match="changed outside UG"):
        if operation == "publish":
            mdm_windows.publish(machine.source, machine.files, machine.policy)
        else:
            mdm_windows.remove()
    assert machine.registry.values["inferenceProvider"] == ("foreign", 1)
    assert machine.plugin.exists()
    assert (machine.runtime / "ownership.json").exists()


def test_registry_failure_rolls_back_and_can_be_retried(machine):
    mdm_windows.publish(machine.source, machine.files, machine.policy)
    before = dict(machine.registry.values)
    machine.registry.fail_write = True
    updated = {**machine.policy, "toolSearchEnabled": "true"}
    with pytest.raises(PermissionError, match="registry write denied"):
        mdm_windows.publish(machine.source, machine.files, updated)
    assert machine.registry.values == before
    assert json.loads((machine.runtime / "ownership.json").read_bytes())["pending_registry"]
    mdm_windows.publish(machine.source, machine.files, updated)
    assert machine.registry.values["toolSearchEnabled"] == ("true", 1)
    assert not json.loads((machine.runtime / "ownership.json").read_bytes())["pending_registry"]


def test_dependency_failure_keeps_registry_and_retries(machine, monkeypatch):
    mdm_windows.publish(machine.source, machine.files, machine.policy)
    before = dict(machine.registry.values)
    replace = managed_files.replace_privileged_bundle
    with monkeypatch.context() as patch:
        patch.setattr(
            managed_files, "replace_privileged_bundle", Mock(side_effect=OSError("locked"))
        )
        with pytest.raises(OSError, match="locked"):
            mdm_windows.publish(
                machine.source, machine.files, {**machine.policy, "toolSearchEnabled": "true"}
            )
    assert managed_files.replace_privileged_bundle is replace
    assert machine.registry.values == before
    mdm_windows.publish(machine.source, machine.files, machine.policy)
    assert mdm_windows.remove()


def test_unowned_plugin_is_not_replaced(machine):
    machine.plugin.mkdir(parents=True)
    (machine.plugin / "foreign.txt").write_text("keep")
    with pytest.raises(RuntimeError, match="no UG ownership"):
        mdm_windows.publish(machine.source, machine.files, machine.policy)
    assert (machine.plugin / "foreign.txt").read_text() == "keep"


def test_link_inside_plugin_is_not_followed_or_deleted(machine, tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("keep")
    (machine.source / "linked").symlink_to(outside)
    with pytest.raises(RuntimeError, match="linked"):
        mdm_windows.publish(machine.source, machine.files, machine.policy)
    assert outside.read_text() == "keep"
    assert "inferenceProvider" not in machine.registry.values


def test_bundle_replacement_restores_previous_directory(machine, monkeypatch):
    mdm_windows.publish(machine.source, machine.files, machine.policy)
    old = (machine.plugin / ".claude-plugin/plugin.json").read_bytes()
    (machine.source / ".claude-plugin/plugin.json").write_text('{"name":"ug","version":"2"}')
    rename = Path.rename

    def fail_new(source, target):
        if source.name == "new":
            raise PermissionError("plugin is locked")
        return rename(source, target)

    monkeypatch.setattr(Path, "rename", fail_new)
    with pytest.raises(PermissionError, match="plugin is locked"):
        mdm_windows.publish(machine.source, machine.files, machine.policy)
    assert (machine.plugin / ".claude-plugin/plugin.json").read_bytes() == old


def test_invalid_ownership_never_removes_registry(machine):
    mdm_windows.publish(machine.source, machine.files, machine.policy)
    (machine.runtime / "ownership.json").write_text(
        '{"version":1,"registry":{"foreign":["x",1]},"pending_registry":{}}'
    )
    before = dict(machine.registry.values)
    with pytest.raises(RuntimeError, match="Invalid Desktop registry ownership"):
        mdm_windows.remove()
    assert machine.registry.values == before


def test_windows_global_setup_propagates_failure(monkeypatch):
    monkeypatch.setattr(setup, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(setup, "machine_wide_only", lambda: True)
    monkeypatch.setattr(mdm, "configure", Mock(side_effect=RuntimeError("publication failed")))
    with pytest.raises(RuntimeError, match="Windows Desktop globally"):
        setup.configure_claude_desktop(
            {"workspace": "https://workspace.example", "profile": "admin"}
        )


def test_windows_requires_elevation(monkeypatch):
    monkeypatch.setattr(mdm, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(mdm.system_cross_os, "is_admin", lambda: False)
    with pytest.raises(PermissionError, match="elevated"):
        mdm.validate_mdm_execution()
    monkeypatch.setattr(mdm.system_cross_os, "is_admin", lambda: True)
    mdm.validate_mdm_execution()


@pytest.fixture
def workspace(machine, monkeypatch):
    runtime = {name: str(machine.root / name / name) for name in ("python", "claude", "databricks")}
    monkeypatch.setattr(mdm, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(mdm, "_runtime", lambda: runtime)
    monkeypatch.setattr(mdm.databricks, "get_databricks_token", lambda *a: "provisioning-secret")
    manifest = {
        "enabled_agents": [
            {"agent": "CODING_AGENT_CLAUDE_CODE", "config": {"tracing": {"enabled": True}}}
        ],
        "mcp_servers": {"names": ["system.ai.github"]},
        "skills": {"names": ["main.team.deferred"]},
    }
    monkeypatch.setattr(
        managed_config, "fetch_managed_coding_agent_configs", lambda *a: ([manifest], None)
    )
    monkeypatch.setattr(
        setup,
        "list_anthropic_model_catalog",
        lambda *a, **kw: AnthropicModelCatalog(
            model_ids=["system.ai.claude-haiku-4-5"], model_id_to_display_name={}
        ),
    )
    monkeypatch.setattr(
        mdm, "_download_skills", Mock(side_effect=AssertionError("Skills are deferred"))
    )
    return manifest, runtime


def test_windows_rendering_uses_consumer_auth_and_has_no_managed_skills(machine, workspace):
    _, runtime = workspace
    mdm.configure("https://workspace.example", "ADMIN_PROFILE")
    policy = machine.registry.values
    args = json.loads(policy["inferenceCredentialHelperArgs"][0])
    assert policy["inferenceCredentialHelper"][0] == runtime["python"]
    assert args[:2] == ["-I", str(machine.runtime / "runtime.py")]
    assert args[-1] == mdm.user_auth_profile("https://workspace.example")
    servers = json.loads(policy["managedMcpServers"][0])
    assert [server["name"] for server in servers] == ["system-ai-github"]
    assert servers[0]["args"][:3] == [*args[:2], "mcp-proxy"]
    hooks = json.loads((machine.plugin / "hooks/hooks.json").read_bytes())
    for event in ("SessionStart", "UserPromptSubmit"):
        command = hooks["hooks"][event][0]["hooks"][0]["command"]
        assert "runtime.py" in command and "-I" in command and "cowork-hook" in command
    assert not (machine.plugin / "skills/deferred").exists()
    assert not (machine.plugin / ".mcp.json").exists()
    manifest = json.loads((machine.plugin / ".claude-plugin/plugin.json").read_bytes())
    assert manifest["installationPreference"] == "required"
    assert (
        json.loads((machine.plugin / "version.json").read_bytes())["version"] == manifest["version"]
    )
    for path in (*machine.runtime.rglob("*"), *machine.plugin.rglob("*")):
        if path.is_file():
            assert b"ADMIN_PROFILE" not in path.read_bytes()
            assert b"provisioning-secret" not in path.read_bytes()


def test_missing_managed_config_retains_previous_deployment(machine, workspace, monkeypatch):
    mdm.configure("https://workspace.example", "admin")
    before = dict(machine.registry.values)
    monkeypatch.setattr(managed_config, "fetch_managed_coding_agent_configs", lambda *a: ([], None))
    with pytest.raises(RuntimeError, match="requires a workspace managed configuration"):
        mdm.configure("https://workspace.example", "admin")
    assert machine.registry.values == before


def test_runtime_discards_provisioning_auth_but_keeps_current_user_home(
    machine, workspace, monkeypatch
):
    _, runtime = workspace
    for key in (
        "DATABRICKS_TOKEN",
        "DATABRICKS_BEARER",
        "DATABRICKS_CONFIG_FILE",
        "DATABRICKS_CONFIG_PROFILE",
        "PYTHONPATH",
        "UCODE_MANAGED_CONFIG_STUB",
    ):
        monkeypatch.setenv(key, "administrator-only")
    monkeypatch.setenv("USERPROFILE", "consumer-home")
    monkeypatch.setenv("PATH", "administrator-path")
    run = Mock()
    monkeypatch.setattr(runpy, "run_module", run)
    exec(mdm._windows_runtime_script(runtime), {})
    run.assert_called_once_with("ucode.cli", run_name="__main__")
    assert os.environ["USERPROFILE"] == "consumer-home"
    assert "administrator-path" not in os.environ["PATH"]
    assert not any(value == "administrator-only" for value in os.environ.values())


@pytest.mark.parametrize("rejected", [False, True])
def test_acl_validation_exchanges_text_with_process(tmp_path, monkeypatch, rejected):
    monkeypatch.setattr(
        windows_machine_paths.system_cross_os, "windows_program_files_dir", lambda: tmp_path
    )
    run = windows_machine_paths.subprocess_cross_os.run
    child = (
        "import json, sys; "
        "assert json.load(sys.stdin); "
        "sys.stderr.write('ACL rejected' if sys.argv[1] == 'True' else ''); "
        "sys.exit(int(sys.argv[1] == 'True'))"
    )

    def acl_process(args, **kwargs):
        assert kwargs["env"]["PSModulePath"] == str(Path(args[0]).parent / "Modules")
        return run([sys.executable, "-c", child, str(rejected)], **kwargs)

    # PowerShell is unavailable on Linux; keep the actual subprocess stream exchange.
    monkeypatch.setattr(windows_machine_paths.subprocess_cross_os, "run", acl_process)
    if rejected:
        with pytest.raises(PermissionError, match="ACL rejected"):
            windows_machine_paths.validate(tmp_path, required=True)
    else:
        windows_machine_paths.validate(tmp_path, required=True)
