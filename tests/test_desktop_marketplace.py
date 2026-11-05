"""User-only Desktop configuration with native registration/cache boundaries simulated."""

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from ucode import config_io, managed_files
from ucode.agents.desktop import claude as desktop
from ucode.agents.desktop import cowork, setup
from ucode.skills_state import SkillInstall, record_downloads, records_for_scope

WORKSPACE = {"workspace": "https://workspace.example", "profile": "chosen"}
SERVERS = [{"name": "slack", "url": "https://workspace.example/mcp/slack", "clients": ["claude"]}]


@pytest.fixture
def system_source(tmp_path, monkeypatch):
    """Use temporary sources and simulate only native registration/cache commands."""
    return desktop_environment(tmp_path, monkeypatch, managed_files.current_os().value)


def desktop_environment(tmp_path, monkeypatch, platform):
    path = tmp_path.resolve() / "system" / "claude-cowork-marketplace"
    monkeypatch.setattr(managed_files, "current_os", lambda: managed_files.OS(platform))
    monkeypatch.setattr(managed_files.system_cross_os, "is_admin", lambda: True)
    monkeypatch.setattr(cowork, "claude_desktop_root_directory", lambda: tmp_path / "Desktop")
    monkeypatch.setattr(cowork, "records_for_scope", lambda *args: [])
    monkeypatch.setattr(cowork.shutil, "which", lambda name: str(tmp_path / "bin" / name))
    root = tmp_path / "Desktop/local-agent-mode-sessions/account/org"
    root.mkdir(parents=True)
    directory = tmp_path / "Native/configLibrary"
    directory.mkdir(parents=True)
    monkeypatch.setattr(desktop, "claude_desktop_directory", lambda: directory)
    _configure_profile(WORKSPACE["workspace"])

    def native_cli(argv, **kwargs):
        # Model only external Claude registration/cache writes; UG and file writers stay real.
        account = Path(kwargs["env"]["CLAUDE_CONFIG_DIR"])
        registry = account / "cowork_plugins"
        registry.mkdir(parents=True, exist_ok=True)
        known_path = registry / "known_marketplaces.json"
        installed_path = registry / "installed_plugins.json"
        known = config_io.read_json_safe(known_path)
        installed = config_io.read_json_safe(installed_path) or {"plugins": {}}
        args = argv[2:]
        output = {"outcome": "ok"}
        if args[:2] == ["marketplace", "add"]:
            source = Path(args[2])
            manifest = json.loads((source / ".claude-plugin/marketplace.json").read_text())
            known[manifest["name"]] = {"source": {"source": "directory", "path": str(source)}}
        elif args[:2] == ["marketplace", "remove"]:
            known.pop(args[2], None)
        elif args[0] in ("install", "update"):
            name, marketplace = args[1].split("@")
            source = Path(known[marketplace]["source"]["path"]) / "plugins" / name
            manifest = json.loads((source / ".claude-plugin/plugin.json").read_text())
            cache = registry / "cache" / marketplace / name / manifest["version"]
            shutil.copytree(source, cache, dirs_exist_ok=True)
            installed["plugins"][args[1]] = [{"installPath": str(cache)}]
        elif args[0] == "uninstall":
            installed["plugins"].pop(args[1], None)
        elif args[0] == "list":
            output = [{"pluginId": name} for name in installed["plugins"]]
        config_io.atomic_write_json(known_path, known)
        config_io.atomic_write_json(installed_path, installed)
        return SimpleNamespace(returncode=0, stdout=json.dumps(output), stderr="")

    monkeypatch.setattr(cowork.subprocess_cross_os, "run", native_cli)
    return path


def _configure_profile(workspace):
    return desktop.configure_claude_desktop(
        model_ids=["system.ai.claude-sonnet-5"],
        base_url=f"{workspace}/ai-gateway/anthropic",
        helper_command="/usr/bin/ug",
        helper_args=["claude-desktop-auth", "--profile", "chosen"],
    )


def _native_servers():
    path = desktop.claude_desktop_directory() / f"{desktop.PROFILE_ID}.json"
    return json.loads(path.read_text())["managedMcpServers"]


def _source(tmp_path, *, plugin_name="ug"):
    source = tmp_path.resolve() / "source"
    (source / ".claude-plugin").mkdir(parents=True)
    (source / ".claude-plugin/marketplace.json").write_text(
        json.dumps(
            {
                "name": cowork.MARKETPLACE_NAME,
                "owner": {"name": "Databricks"},
                "plugins": [{"name": plugin_name, "source": f"./plugins/{plugin_name}"}],
            }
        )
    )
    plugin = source / "plugins" / plugin_name
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin/plugin.json").write_text(
        json.dumps({"name": plugin_name, "version": "1.0.0"})
    )
    (plugin / "skills/review").mkdir(parents=True)
    (plugin / "skills/review/SKILL.md").write_text("Review carefully")
    (plugin / "skills/review/image.bin").write_bytes(b"\x00\xff\x80")
    executable = plugin / "skills/review/run.sh"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    return source


def _installed_source(*, other_plugins=(), plugin_id=cowork.PLUGIN_ID):
    root = cowork.discover_cowork_root()
    installed = config_io.read_json_safe(root / "cowork_plugins/installed_plugins.json")
    assert set(installed["plugins"]) == {plugin_id, *other_plugins}
    entries = installed["plugins"][plugin_id]
    assert len(entries) == 1
    return Path(entries[0]["installPath"])


@pytest.mark.parametrize("scope", ["global", "user"], ids=["default", "user"])
def test_ordinary_configure_writes_only_user_source_and_one_install(system_source, scope):
    managed_files.set_config_scope(scope)
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    plugin = cowork._marketplace_root() / "plugins/ug"
    assert not (plugin / ".mcp.json").exists()
    mcp = cowork._cowork_mcp_config(SERVERS, **WORKSPACE)
    assert _native_servers() == [
        {"name": name, "transport": "stdio", **{k: v for k, v in entry.items() if k != "type"}}
        for name, entry in mcp["mcpServers"].items()
    ]
    assert json.loads((plugin / "hooks/hooks.json").read_text()) == cowork._connector_hooks(
        mcp, **WORKSPACE, use_pat=False
    )
    assert (
        cowork._registered_marketplace_source(cowork.discover_cowork_root())
        == cowork._marketplace_root()
    )
    assert cowork._marketplace_files(_installed_source()) == cowork._marketplace_files(plugin)
    assert not (plugin / "version.json").exists()
    assert "installationPreference" not in json.loads(
        (plugin / ".claude-plugin/plugin.json").read_text()
    )
    before = cowork._marketplace_files(cowork._marketplace_root())
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    assert cowork._marketplace_files(cowork._marketplace_root()) == before
    _installed_source()
    assert cowork.revert_cowork()
    assert not cowork._marketplace_root().exists()
    assert not cowork._state_path().exists()


@pytest.mark.parametrize("scope", ["global", "user"])
@pytest.mark.parametrize("organization_plugin", [False, True])
def test_first_configure_skips_uninitialized_cowork(
    system_source, tmp_path, monkeypatch, scope, organization_plugin
):
    managed_files.set_config_scope(scope)
    shutil.rmtree(tmp_path / "Desktop")
    if organization_plugin:
        organization = tmp_path / "org-plugins/ug"
        organization.mkdir(parents=True)
        monkeypatch.setattr(managed_files, "claude_desktop_org_plugin_path", lambda: organization)
    if organization_plugin:
        with pytest.raises(RuntimeError, match="organization plugin already exists"):
            cowork.configure_cowork(WORKSPACE, SERVERS)
    else:
        assert not cowork.configure_cowork(WORKSPACE, SERVERS)
        assert [server["name"] for server in _native_servers()] == ["slack"]
    assert not cowork._marketplace_root().exists()
    assert not cowork._state_path().exists()


def test_existing_root_registration_switches_to_user_without_duplicates(system_source, tmp_path):
    shutil.copytree(_source(tmp_path), system_source)
    root = cowork.discover_cowork_root()
    cowork._install_plugin(root, system_source)
    config_io.atomic_write_json(
        cowork._state_path(),
        {
            "version": 1,
            "cowork_root": str(root),
            "marketplace_root": str(system_source),
        },
    )
    registry = root / "cowork_plugins/installed_plugins.json"
    data = config_io.read_json_safe(registry)
    data["plugins"]["personal@other"] = [{"installPath": "untouched"}]
    config_io.atomic_write_json(registry, data)
    before = cowork._marketplace_files(system_source)
    for scope in ("global", "user", "global"):
        managed_files.set_config_scope(scope)
        assert cowork.configure_cowork(WORKSPACE, SERVERS)
        assert cowork._registered_marketplace_source(root) == cowork._marketplace_root()
        installed = config_io.read_json_safe(registry)["plugins"]
        assert set(installed) == {cowork.PLUGIN_ID, "personal@other"}
        assert installed["personal@other"] == [{"installPath": "untouched"}]
        assert cowork._marketplace_files(system_source) == before
    assert cowork.revert_cowork()
    assert cowork._marketplace_files(system_source) == before
    assert config_io.read_json_safe(registry)["plugins"] == {
        "personal@other": [{"installPath": "untouched"}]
    }


def test_ordinary_configure_needs_no_root_write_permission(system_source, monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("ordinary Desktop configure attempted a privileged write")

    monkeypatch.setattr(managed_files, "_session_worker", denied)
    monkeypatch.setattr(managed_files.system_cross_os, "is_admin", lambda: False)
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    assert (
        cowork._registered_marketplace_source(cowork.discover_cowork_root())
        == cowork._marketplace_root()
    )
    assert cowork.revert_cowork()


@pytest.mark.parametrize("configured", [False, True])
def test_dry_run_leaves_state_and_bundle_unchanged(system_source, configured):
    if configured:
        assert cowork.configure_cowork(WORKSPACE, SERVERS)
    source_before = cowork._marketplace_files(cowork._marketplace_root())
    registration_before = cowork._marketplace_files(cowork.discover_cowork_root())
    state_before = cowork._state_path().read_bytes() if cowork._state_path().exists() else None
    config_io.set_dry_run(True)
    try:
        setup.configure_claude_cowork(WORKSPACE, SERVERS)
        assert not cowork.revert_cowork()
        assert cowork._marketplace_files(cowork._marketplace_root()) == source_before
        assert cowork._marketplace_files(cowork.discover_cowork_root()) == registration_before
        state_after = cowork._state_path().read_bytes() if cowork._state_path().exists() else None
        assert state_after == state_before
    finally:
        config_io.set_dry_run(False)


def test_failed_previous_account_cleanup_can_be_retried(system_source, tmp_path, monkeypatch):
    old = tmp_path / "old"
    config_io.atomic_write_json(cowork._state_path(), {"version": 1, "cowork_root": str(old)})
    remove = cowork._remove_plugin_registration

    def fail_old(root):
        if root == old:
            raise RuntimeError("previous account cleanup failed")
        return remove(root)

    monkeypatch.setattr(cowork, "_remove_plugin_registration", fail_old)
    with pytest.raises(RuntimeError, match="cleanup failed"):
        cowork.configure_cowork(WORKSPACE, SERVERS)
    assert config_io.read_json_safe(cowork._state_path()) == {
        "version": 1,
        "cowork_root": str(old),
    }
    assert not cowork._marketplace_root().exists()
    monkeypatch.setattr(cowork, "_remove_plugin_registration", remove)
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    assert config_io.read_json_safe(cowork._state_path()) == {
        "version": 1,
        "cowork_root": str(cowork.discover_cowork_root()),
    }
    assert config_io.read_json_safe(old / "cowork_plugins/installed_plugins.json")["plugins"] == {}


def test_downloaded_skills_follow_workspace_selection(system_source, tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(cowork, "records_for_scope", records_for_scope)
    for name, workspace in (
        ("selected", WORKSPACE["workspace"]),
        ("foreign", "https://other.example"),
    ):
        skill = home / ".claude/skills" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(name)
        (skill / "asset.bin").write_bytes(b"\xff\x00")
        record_downloads(
            [
                SkillInstall(
                    fqn=f"main.default.{name}",
                    bundle_name=name,
                    workspace=workspace,
                    scope="user",
                    base=str(home),
                    dirs=(str(skill),),
                )
            ]
        )
    assert cowork.configure_cowork(WORKSPACE, [])
    assert (
        cowork._marketplace_root() / "plugins/ug/skills/selected/SKILL.md"
    ).read_text() == "selected"
    assert (
        cowork._marketplace_root() / "plugins/ug/skills/selected/asset.bin"
    ).read_bytes() == b"\xff\x00"
    assert not (cowork._marketplace_root() / "plugins/ug/skills/foreign").exists()
    version_before = json.loads((_installed_source() / ".claude-plugin/plugin.json").read_text())[
        "version"
    ]
    (home / ".claude/skills/selected/asset.bin").write_bytes(b"asset-only update")
    assert cowork.configure_cowork(WORKSPACE, [])
    installed = _installed_source()
    assert (installed / "skills/selected/asset.bin").read_bytes() == b"asset-only update"
    assert (
        json.loads((installed / ".claude-plugin/plugin.json").read_text())["version"]
        != version_before
    )
    _configure_profile("https://other.example")
    assert cowork.configure_cowork({**WORKSPACE, "workspace": "https://other.example"}, [])
    assert (
        cowork._marketplace_root() / "plugins/ug/skills/foreign/SKILL.md"
    ).read_text() == "foreign"
    assert not (cowork._marketplace_root() / "plugins/ug/skills/selected").exists()


@pytest.mark.parametrize("recovery", ["retry", "revert"])
def test_failed_refresh_retains_generated_sources_for_recovery(
    system_source, monkeypatch, recovery
):
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    user_before = cowork._marketplace_files(cowork._marketplace_root())
    connectors_before = _native_servers()
    _configure_profile(WORKSPACE["workspace"])
    managed_files.set_config_scope("global")
    run = cowork.subprocess_cross_os.run
    failed = False

    def fail_refresh(argv, **kwargs):
        nonlocal failed
        if not failed and argv[1:3] in (["plugin", "install"], ["plugin", "update"]):
            failed = True
            # A failed native command can have already changed its cache and registry.
            run(argv, **kwargs)
            return SimpleNamespace(returncode=1, stdout="", stderr="refresh failed")
        return run(argv, **kwargs)

    monkeypatch.setattr(cowork.subprocess_cross_os, "run", fail_refresh)
    with pytest.raises(RuntimeError, match="refresh failed"):
        cowork.configure_cowork(WORKSPACE, [{**SERVERS[0], "name": "changed"}])
    assert _native_servers() == connectors_before
    generated = cowork._marketplace_files(cowork._marketplace_root())
    assert generated != user_before
    state = config_io.read_json_safe(cowork._state_path())
    assert state["cowork_root"] == str(cowork.discover_cowork_root())
    assert "system_digest" not in state
    assert "pending_system_digest" not in state
    if recovery == "revert":
        assert cowork.revert_cowork()
        assert not cowork._marketplace_root().exists()
        assert not cowork._state_path().exists()
        return
    assert cowork.configure_cowork(WORKSPACE, [{**SERVERS[0], "name": "changed"}])
    assert cowork._marketplace_files(_installed_source()) == cowork._marketplace_files(
        cowork._marketplace_root() / "plugins/ug"
    )
    assert (
        cowork._registered_marketplace_source(cowork.discover_cowork_root())
        == cowork._marketplace_root()
    )


@pytest.mark.parametrize("failure", ["exit", "timeout"])
def test_legacy_cleanup_failure_keeps_updated_plugin_and_native_connectors(
    system_source, monkeypatch, capsys, failure
):
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    (_installed_source() / ".mcp.json").write_text(
        json.dumps(cowork._cowork_mcp_config(SERVERS, **WORKSPACE))
    )
    registry = cowork.discover_cowork_root() / "cowork_plugins/installed_plugins.json"
    installed = config_io.read_json_safe(registry)
    installed["plugins"][cowork.LEGACY_PLUGIN_ID] = [{"installPath": "old-plugin"}]
    installed["plugins"]["other@unrelated"] = [{"installPath": "unrelated-plugin"}]
    config_io.atomic_write_json(registry, installed)
    _configure_profile(WORKSPACE["workspace"])
    run = cowork.subprocess_cross_os.run

    def fail_legacy_cleanup(argv, **kwargs):
        if argv[1:4] == ["plugin", "uninstall", cowork.LEGACY_PLUGIN_ID]:
            if failure == "timeout":
                raise subprocess.TimeoutExpired(argv, 120)
            return SimpleNamespace(returncode=1, stdout="", stderr="cleanup failed")
        return run(argv, **kwargs)

    monkeypatch.setattr(cowork.subprocess_cross_os, "run", fail_legacy_cleanup)
    servers = [{**SERVERS[0], "name": "github"}]
    assert cowork.configure_cowork(WORKSPACE, servers)
    assert [server["name"] for server in _native_servers()] == ["github"]
    plugin = _installed_source(other_plugins=(cowork.LEGACY_PLUGIN_ID, "other@unrelated"))
    assert not (plugin / ".mcp.json").exists()
    assert cowork._marketplace_files(plugin) == cowork._marketplace_files(
        cowork._marketplace_root() / "plugins/ug"
    )
    output = " ".join(capsys.readouterr().out.split())
    assert "Could not remove legacy Claude Desktop plugin" in output
    assert "Run `ug configure` again to retry cleanup" in output

    monkeypatch.setattr(cowork.subprocess_cross_os, "run", run)
    assert cowork.configure_cowork(WORKSPACE, servers)
    _installed_source(other_plugins=("other@unrelated",))
    assert [server["name"] for server in _native_servers()] == ["github"]


def test_changed_payload_updates_user_source_and_installed_version(system_source):
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    before = json.loads((_installed_source() / ".claude-plugin/plugin.json").read_text())["version"]
    assert cowork.configure_cowork(WORKSPACE, [{**SERVERS[0], "name": "changed"}])
    after = json.loads((_installed_source() / ".claude-plugin/plugin.json").read_text())["version"]
    assert after != before
    assert cowork._marketplace_files(_installed_source()) == cowork._marketplace_files(
        cowork._marketplace_root() / "plugins/ug"
    )
    assert not (_installed_source() / ".mcp.json").exists()
    assert [server["name"] for server in _native_servers()] == ["changed"]
    assert cowork.configure_cowork(WORKSPACE, [])
    installed = _installed_source()
    assert not (installed / ".mcp.json").exists()
    assert not (installed / "hooks").exists()
    assert not (installed / "skills").exists()
    assert cowork._marketplace_files(installed) == cowork._marketplace_files(
        cowork._marketplace_root() / "plugins/ug"
    )


@pytest.mark.parametrize("scope", ["user", "global"])
def test_organization_loader_copy_blocks_duplicate_before_writes(
    system_source, tmp_path, monkeypatch, scope
):
    organization = tmp_path / "org-plugins/ug"
    organization.mkdir(parents=True)
    (organization / "keep").write_text("older pilot")
    monkeypatch.setattr(managed_files, "claude_desktop_org_plugin_path", lambda: organization)
    managed_files.set_config_scope(scope)
    with pytest.raises(RuntimeError, match="organization plugin already exists"):
        cowork.configure_cowork(WORKSPACE, SERVERS)
    assert not cowork._marketplace_root().exists()
    assert (organization / "keep").read_text() == "older pilot"


def test_explicit_global_leaves_ordinary_cowork_files_untouched(system_source):
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    source = cowork._marketplace_files(cowork._marketplace_root())
    registry = cowork._marketplace_files(cowork.discover_cowork_root())
    state = cowork._state_path().read_bytes()
    managed_files.set_machine_wide_only(True)
    setup.configure_claude_cowork(WORKSPACE, [{**SERVERS[0], "name": "changed"}])
    assert cowork._marketplace_files(cowork._marketplace_root()) == source
    assert cowork._marketplace_files(cowork.discover_cowork_root()) == registry
    assert cowork._state_path().read_bytes() == state


@pytest.mark.parametrize(
    "contents",
    [
        "{",
        "[]",
        "null",
        '{"unity-gateway-cowork": []}',
        '{"unity-gateway-cowork": {"source": null}}',
        '{"unity-gateway-cowork": {"source": {"source": "directory"}}}',
        '{"unity-gateway-cowork": {"source": {"source": "directory", "path": null}}}',
        '{"unity-gateway-cowork": {"source": {"source": "directory", "path": 123}}}',
        '{"unity-gateway-cowork": {"source": {"source": "directory", "path": ""}}}',
    ],
)
def test_malformed_marketplace_metadata_is_ignored(system_source, contents):
    root = cowork.discover_cowork_root()
    registry = root / "cowork_plugins/known_marketplaces.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(contents)
    assert cowork._registered_marketplace_source(root) is None
    assert registry.read_text() == contents


@pytest.mark.parametrize(
    "source",
    [{"source": "github", "repo": "example/plugins"}, {"source": "directory", "path": "relative"}],
)
def test_nonlocal_marketplace_registration_is_preserved(system_source, source):
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    root = cowork.discover_cowork_root()
    registry = root / "cowork_plugins/known_marketplaces.json"
    config_io.atomic_write_json(registry, {cowork.MARKETPLACE_NAME: {"source": source}})
    before = cowork._marketplace_files(root)
    with pytest.raises(RuntimeError, match="not a local directory; left unchanged"):
        cowork.configure_cowork(WORKSPACE, SERVERS)
    assert cowork._marketplace_files(root) == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory aliases")
@pytest.mark.parametrize("aliased_path", ["source", "account"])
def test_equivalent_paths_do_not_remove_existing_registration(
    system_source, tmp_path, monkeypatch, aliased_path
):
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    root = cowork.discover_cowork_root()
    original = cowork._marketplace_root() if aliased_path == "source" else root
    alias = tmp_path / "directory-alias"
    alias.symlink_to(original, target_is_directory=True)
    if aliased_path == "source":
        registry = root / "cowork_plugins/known_marketplaces.json"
        known = config_io.read_json_safe(registry)
        known[cowork.MARKETPLACE_NAME]["source"]["path"] = str(alias)
        config_io.atomic_write_json(registry, known)
    else:
        config_io.atomic_write_json(cowork._state_path(), {"version": 1, "cowork_root": str(alias)})

    def unexpected_removal(*args, **kwargs):
        pytest.fail("equivalent directory paths caused plugin removal")

    monkeypatch.setattr(cowork, "_remove_plugin_registration", unexpected_removal)
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    _installed_source()


@pytest.mark.parametrize("failure", ["add", "install", "restore"])
@pytest.mark.parametrize("previous_plugin_id", [cowork.PLUGIN_ID, cowork.LEGACY_PLUGIN_ID])
def test_failed_source_switch_restores_previous_plugin(
    system_source, tmp_path, monkeypatch, failure, previous_plugin_id
):
    previous_name = previous_plugin_id.split("@")[0]
    shutil.copytree(_source(tmp_path, plugin_name=previous_name), system_source)
    root = cowork.discover_cowork_root()
    cowork._install_plugin(root, system_source, plugin_id=previous_plugin_id)
    old_payload = cowork._marketplace_files(_installed_source(plugin_id=previous_plugin_id))
    registry = root / "cowork_plugins/installed_plugins.json"
    installed = config_io.read_json_safe(registry)
    installed["plugins"]["personal@other"] = [{"installPath": "untouched"}]
    config_io.atomic_write_json(registry, installed)
    native = cowork.subprocess_cross_os.run
    failed = False

    def fail_new_install(argv, **kwargs):
        nonlocal failed
        if failure == "restore" and failed and argv[1:4] == ["plugin", "marketplace", "add"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="restoration failed")
        command = (
            ["plugin", "marketplace", "add"]
            if failure == "add"
            else ["plugin", "install", cowork.PLUGIN_ID]
        )
        if not failed and argv[1:4] == command:
            failed = True
            native(argv, **kwargs)
            return SimpleNamespace(returncode=1, stdout="", stderr="replacement failed")
        return native(argv, **kwargs)

    monkeypatch.setattr(cowork.subprocess_cross_os, "run", fail_new_install)
    message = (
        "Could not restore.*restoration failed.*Run `ug configure`"
        if failure == "restore"
        else "replacement failed"
    )
    with pytest.raises(RuntimeError, match=message):
        cowork.configure_cowork(WORKSPACE, SERVERS)
    installed = config_io.read_json_safe(registry)["plugins"]
    assert installed["personal@other"] == [{"installPath": "untouched"}]
    if failure != "restore":
        assert set(installed) == {previous_plugin_id, "personal@other"}
        assert cowork._registered_marketplace_source(root) == system_source
        assert (
            cowork._marketplace_files(
                _installed_source(other_plugins=("personal@other",), plugin_id=previous_plugin_id)
            )
            == old_payload
        )
    assert cowork._state_path().exists()
    monkeypatch.setattr(cowork.subprocess_cross_os, "run", native)
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    assert cowork._registered_marketplace_source(root) == cowork._marketplace_root()
    assert cowork._marketplace_files(
        _installed_source(other_plugins=("personal@other",))
    ) == cowork._marketplace_files(cowork._marketplace_root() / "plugins/ug")
