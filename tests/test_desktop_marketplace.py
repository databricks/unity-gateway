"""System Desktop bundles, migration, and preservation without a native Desktop process."""

import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from ucode import config_io, managed_files
from ucode.agents.desktop import cowork, system_marketplace
from ucode.skills_state import SkillInstall, record_downloads, records_for_scope

WORKSPACE = {"workspace": "https://workspace.example", "profile": "chosen"}
SERVERS = [{"name": "slack", "url": "https://workspace.example/mcp/slack", "clients": ["claude"]}]


@pytest.fixture(
    params=[
        "windows",
        pytest.param(
            "macos", marks=pytest.mark.skipif(os.name == "nt", reason="POSIX shell worker")
        ),
    ]
)
def system_source(tmp_path, monkeypatch, request):
    """Use temporary destinations; exercise the real filesystem transaction for each backend."""
    path = tmp_path.resolve() / "system" / "claude-cowork-marketplace"
    monkeypatch.setattr(managed_files, "claude_desktop_marketplace_path", lambda: path)
    monkeypatch.setattr(managed_files, "current_os", lambda: managed_files.OS(request.param))
    monkeypatch.setattr(managed_files.system_cross_os, "is_admin", lambda: True)
    monkeypatch.setattr(cowork, "claude_desktop_root_directory", lambda: tmp_path / "Desktop")
    monkeypatch.setattr(cowork, "records_for_scope", lambda *args: [])
    monkeypatch.setattr(cowork.shutil, "which", lambda name: str(tmp_path / "bin" / name))
    if request.param == "macos":
        # Run the packaged shell on Linux/macOS without sudo, with only target/OS adapters changed.
        platform = "macos" if os.uname().sysname == "Darwin" else "linux"
        script = managed_files._SUDO_REPLACE_SCRIPT.replace(
            "macos:/Library/Application Support/UnityGateway/claude-cowork-marketplace",
            f"{platform}:{path}",
        )
        if platform == "linux":
            script = script.replace("/usr/bin/stat -f %u", "stat -c %u")
        script = script.replace('\ncase "$mode" in', '\nchown() { :; }\ncase "$mode" in')
        script = script.replace(
            '\ncase "$mode" in',
            """
mv() {
    case "$1" in
        */request/new)
            if [ "${UG_TEST_BUNDLE_FAIL:-}" = rename ]; then return 73; fi ;;
    esac
    command mv "$@"
}
case "$mode" in""",
        )

        monkeypatch.setattr(
            managed_files,
            "_sudo_replace_command",
            lambda mode: [
                "/usr/bin/env",
                f"SUDO_UID={os.getuid()}",
                "/bin/sh",
                "-c",
                script,
                "bundle-test",
                mode,
                platform,
            ],
        )
    root = tmp_path / "Desktop/local-agent-mode-sessions/account/org"
    root.mkdir(parents=True)

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
            known.setdefault(
                manifest["name"], {"source": {"source": "directory", "path": str(source)}}
            )
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


def _source(tmp_path):
    source = tmp_path.resolve() / "source"
    (source / ".claude-plugin").mkdir(parents=True)
    (source / ".claude-plugin/marketplace.json").write_text(
        json.dumps(
            {
                "name": cowork.MARKETPLACE_NAME,
                "owner": {"name": "Databricks"},
                "plugins": [{"name": "ug", "source": "./plugins/ug"}],
            }
        )
    )
    plugin = source / "plugins/ug"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin/plugin.json").write_text('{"name":"ug", "version":"1.0.0"}')
    (plugin / "skills/review").mkdir(parents=True)
    (plugin / "skills/review/SKILL.md").write_text("Review carefully")
    (plugin / "skills/review/image.bin").write_bytes(b"\x00\xff\x80")
    executable = plugin / "skills/review/run.sh"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    return source


def _installed_source():
    root = cowork.discover_cowork_root()
    installed = config_io.read_json_safe(root / "cowork_plugins/installed_plugins.json")
    assert list(installed["plugins"]) == [cowork.PLUGIN_ID]
    entries = installed["plugins"][cowork.PLUGIN_ID]
    assert len(entries) == 1
    return Path(entries[0]["installPath"])


def test_install_update_noop_and_revert_preserve_other_plugins(system_source, tmp_path):
    source = _source(tmp_path)
    state = config_io.APP_DIR / "test-state.json"
    unrelated = system_source.parent / "personal"
    unrelated.mkdir(parents=True)
    (unrelated / "keep").write_text("personal plugin")

    system_marketplace.install(source, state)
    first = system_marketplace.read_bundle(system_source)
    manifest = json.loads((system_source / "plugins/ug/.claude-plugin/plugin.json").read_text())
    assert manifest == {"name": "ug", "version": "1.0.0"}
    assert not (system_source / "version.json").exists()
    assert system_marketplace.read_bundle(source) == first
    assert (system_source / "plugins/ug/skills/review/image.bin").read_bytes() == b"\x00\xff\x80"
    if os.name != "nt":
        assert (system_source / "plugins/ug/skills/review/run.sh").stat().st_mode & 0o777 == 0o755
    timestamp = system_source.stat().st_mtime_ns
    system_marketplace.install(source, state)
    assert system_marketplace.read_bundle(system_source) == first
    assert system_source.stat().st_mtime_ns == timestamp

    (source / "plugins/ug/skills/review/image.bin").unlink()
    (source / "plugins/ug/skills/review/SKILL.md").write_text("Updated skill")
    system_marketplace.install(source, state)
    assert not (system_source / "plugins/ug/skills/review/image.bin").exists()
    assert (system_source / "plugins/ug/skills/review/SKILL.md").read_text() == "Updated skill"
    assert system_marketplace.read_bundle(source) == system_marketplace.read_bundle(system_source)
    assert system_marketplace.remove(state)
    assert not system_source.exists()
    assert not system_marketplace.remove(state)
    assert (unrelated / "keep").read_text() == "personal plugin"


def test_foreign_or_edited_installation_is_preserved(system_source, tmp_path):
    source = _source(tmp_path)
    state = config_io.APP_DIR / "test-state.json"
    system_source.mkdir(parents=True)
    custom = system_source / "it-owned.txt"
    custom.write_text("IT installation")
    with pytest.raises(RuntimeError, match="not this user's UG installation"):
        system_marketplace.install(source, state)
    assert not system_marketplace.remove(state)
    assert custom.read_text() == "IT installation"
    custom.unlink()
    system_source.rmdir()
    system_marketplace.install(source, state)
    (system_source / "plugins/ug/skills/review/SKILL.md").write_text("External edit")
    before = state.read_bytes()
    with pytest.raises(RuntimeError, match="edited outside UG"):
        system_marketplace.remove(state)
    assert (system_source / "plugins/ug/skills/review/SKILL.md").read_text() == "External edit"
    assert state.read_bytes() == before


def test_global_configure_writes_both_sources_and_one_user_install(system_source):
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    assert system_marketplace.read_bundle(system_source) == system_marketplace.read_bundle(
        cowork._marketplace_root()
    )
    plugin = system_source / "plugins/ug"
    mcp = json.loads((plugin / ".mcp.json").read_text())
    assert mcp == cowork._cowork_mcp_config(SERVERS, **WORKSPACE)
    hooks = json.loads((plugin / "hooks/hooks.json").read_text())
    assert hooks == cowork._connector_hooks(mcp, **WORKSPACE, use_pat=False)
    assert cowork._registered_marketplace_source(cowork.discover_cowork_root()) == system_source
    assert system_marketplace.read_bundle(_installed_source()) == system_marketplace.read_bundle(
        plugin
    )
    assert not (plugin / "version.json").exists()
    assert "installationPreference" not in json.loads(
        (plugin / ".claude-plugin/plugin.json").read_text()
    )
    before = system_marketplace.read_bundle(system_source)
    timestamp = system_source.stat().st_mtime_ns
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    assert system_marketplace.read_bundle(system_source) == before
    assert system_source.stat().st_mtime_ns == timestamp
    _installed_source()
    assert cowork.revert_cowork()
    assert not system_source.exists()
    assert not cowork._marketplace_root().exists()
    assert not cowork._state_path().exists()


def test_global_needs_cowork_account_before_writes(system_source, tmp_path):
    shutil.rmtree(tmp_path / "Desktop")
    with pytest.raises(RuntimeError, match="start Claude Cowork"):
        cowork.configure_cowork(WORKSPACE, SERVERS)
    assert not system_source.exists()
    assert not cowork._marketplace_root().exists()


def test_scope_switches_keep_one_registration_and_preserve_unrelated_plugin(system_source):
    managed_files.set_config_scope("user")
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    root = cowork.discover_cowork_root()
    registry = root / "cowork_plugins/installed_plugins.json"
    data = config_io.read_json_safe(registry)
    data["plugins"]["personal@other"] = [{"installPath": "untouched"}]
    config_io.atomic_write_json(registry, data)
    for scope in ("global", "user", "global"):
        managed_files.set_config_scope(scope)
        assert cowork.configure_cowork(WORKSPACE, SERVERS)
        expected = system_source if scope == "global" else cowork._marketplace_root()
        assert cowork._registered_marketplace_source(root) == expected
        installed = config_io.read_json_safe(registry)["plugins"]
        assert set(installed) == {cowork.PLUGIN_ID, "personal@other"}
        assert installed["personal@other"] == [{"installPath": "untouched"}]
        assert system_marketplace.read_bundle(
            cowork._marketplace_root()
        ) == system_marketplace.read_bundle(system_source)
    assert cowork.revert_cowork()
    assert config_io.read_json_safe(registry)["plugins"] == {
        "personal@other": [{"installPath": "untouched"}]
    }


def test_install_failure_preserves_user_registration_and_state(
    system_source, tmp_path, monkeypatch
):
    original = {"version": 1, "cowork_root": str(tmp_path / "old")}
    config_io.atomic_write_json(cowork._state_path(), original)
    cowork._marketplace_root().mkdir()
    marker = cowork._marketplace_root() / "keep"
    marker.write_text("original source")

    def denied(*args, **kwargs):
        raise PermissionError("permission denied")

    if managed_files.current_os() is managed_files.OS.WINDOWS:
        monkeypatch.setattr(managed_files.system_cross_os, "is_admin", lambda: False)
    else:
        monkeypatch.setattr(managed_files, "replace_privileged_bundle", denied)
    with pytest.raises((RuntimeError, PermissionError)):
        cowork.configure_cowork(WORKSPACE, SERVERS)
    assert config_io.read_json_safe(cowork._state_path()) == original
    assert marker.read_text() == "original source"
    assert not system_source.exists()


@pytest.mark.parametrize("opt_out", ["user", "environment"])
def test_opt_out_keeps_root_source_untouched(system_source, monkeypatch, opt_out):
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    before = system_marketplace.read_bundle(system_source)
    if opt_out == "user":
        managed_files.set_config_scope("user")
    else:
        monkeypatch.setenv("UG_SKIP_OS_SETTINGS", "1")
    assert cowork.configure_cowork(WORKSPACE, [])
    assert system_marketplace.read_bundle(system_source) == before
    assert (
        cowork._registered_marketplace_source(cowork.discover_cowork_root())
        == cowork._marketplace_root()
    )
    assert not (_installed_source() / ".mcp.json").exists()
    assert cowork.revert_cowork()
    assert system_marketplace.read_bundle(system_source) == before
    assert config_io.read_json_safe(cowork._state_path())["system_digest"]
    monkeypatch.delenv("UG_SKIP_OS_SETTINGS", raising=False)
    managed_files.set_config_scope("global")
    assert cowork.revert_cowork()
    assert not system_source.exists()
    assert not cowork._state_path().exists()


def test_dry_run_leaves_no_state_or_bundle(system_source):
    config_io.set_dry_run(True)
    try:
        assert not cowork.configure_cowork(WORKSPACE, SERVERS)
        assert not cowork.revert_cowork()
        assert not system_source.exists()
        assert not cowork._state_path().exists()
    finally:
        config_io.set_dry_run(False)


def test_pending_installation_can_be_reverted(system_source):
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    state = config_io.read_json_safe(cowork._state_path())
    state["pending_system_digest"] = state.pop("system_digest")
    config_io.atomic_write_json(cowork._state_path(), state)
    assert cowork.revert_cowork()
    assert not system_source.exists()


def test_failed_update_restores_previous_bundle(system_source, tmp_path, monkeypatch):
    source = _source(tmp_path)
    state = config_io.APP_DIR / "test-state.json"
    system_marketplace.install(source, state)
    before = system_marketplace.read_bundle(system_source)
    previous_state = state.read_bytes()
    (source / "plugins/ug/skills/review/SKILL.md").write_text("Updated skill")
    if managed_files.current_os() is managed_files.OS.WINDOWS:
        replace = os.replace

        def fail_new(source, target):
            if Path(source).name == "new":
                raise PermissionError("simulated rename failure")
            return replace(source, target)

        monkeypatch.setattr(os, "replace", fail_new)
    else:
        monkeypatch.setenv("UG_TEST_BUNDLE_FAIL", "rename")
    with pytest.raises((RuntimeError, PermissionError)):
        system_marketplace.install(source, state)
    assert system_marketplace.read_bundle(system_source) == before
    assert state.read_bytes() == previous_state
    assert not list(system_source.parent.glob(".ug-install*"))


def test_concurrent_edit_is_not_overwritten(system_source, tmp_path):
    source = _source(tmp_path)
    state = config_io.APP_DIR / "test-state.json"
    system_marketplace.install(source, state)
    previous = system_marketplace.read_bundle(system_source)
    (system_source / "plugins/ug/skills/review/SKILL.md").write_text("Concurrent edit")
    with pytest.raises(RuntimeError):
        system_marketplace._update_bundle(system_source, None, previous)
    assert (system_source / "plugins/ug/skills/review/SKILL.md").read_text() == "Concurrent edit"


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
    pending = config_io.read_json_safe(cowork._state_path())
    assert pending["previous_cowork_roots"] == [str(old)]
    assert pending["cowork_root"] == str(cowork.discover_cowork_root())
    assert pending["system_digest"] == system_marketplace.bundle_digest(
        system_marketplace.read_bundle(system_source)
    )
    monkeypatch.setattr(cowork, "_remove_plugin_registration", remove)
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    assert "previous_cowork_roots" not in config_io.read_json_safe(cowork._state_path())


@pytest.mark.parametrize("opt_out", ["user", "environment"])
def test_opt_out_uses_existing_user_installer(system_source, tmp_path, monkeypatch, opt_out):
    if opt_out == "user":
        managed_files.set_config_scope("user")
    else:
        monkeypatch.setenv("UG_SKIP_OS_SETTINGS", "1")
    root = tmp_path / "Desktop/local-agent-mode-sessions/account/org"
    root.mkdir(parents=True, exist_ok=True)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout='{"outcome":"ok"}', stderr="")

    monkeypatch.setattr(cowork.subprocess_cross_os, "run", run)
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    assert any(argv[1:4] == ["plugin", "install", cowork.PLUGIN_ID] for argv in calls)
    assert config_io.read_json_safe(cowork._state_path())["cowork_root"] == str(root)
    assert not system_source.exists()


def test_removing_all_contents_updates_existing_system_source(system_source):
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    assert cowork.configure_cowork(WORKSPACE, [])
    assert (system_source / "plugins/ug/.claude-plugin/plugin.json").is_file()
    assert not (system_source / "plugins/ug/.mcp.json").exists()
    assert not (system_source / "plugins/ug/hooks").exists()
    assert not (system_source / "plugins/ug/skills").exists()


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
    assert (system_source / "plugins/ug/skills/selected/SKILL.md").read_text() == "selected"
    assert (system_source / "plugins/ug/skills/selected/asset.bin").read_bytes() == b"\xff\x00"
    assert not (system_source / "plugins/ug/skills/foreign").exists()
    assert cowork.configure_cowork({**WORKSPACE, "workspace": "https://other.example"}, [])
    assert (system_source / "plugins/ug/skills/foreign/SKILL.md").read_text() == "foreign"
    assert not (system_source / "plugins/ug/skills/selected").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX temporary-directory symlink")
def test_system_configure_accepts_os_temporary_directory_alias(
    system_source, tmp_path, monkeypatch
):
    physical = tmp_path / "physical-temp"
    physical.mkdir()
    alias = tmp_path / "temp-alias"
    alias.symlink_to(physical, target_is_directory=True)
    monkeypatch.setattr(cowork.tempfile, "tempdir", str(alias))
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    assert (system_source / "plugins/ug/.mcp.json").is_file()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink input")
def test_system_source_link_is_preserved(system_source, tmp_path):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "keep").write_text("untouched")
    system_source.parent.mkdir(parents=True, exist_ok=True)
    system_source.symlink_to(foreign, target_is_directory=True)
    with pytest.raises(RuntimeError, match="linked Desktop plugin path"):
        cowork.configure_cowork(WORKSPACE, SERVERS)
    assert system_source.is_symlink()
    assert (foreign / "keep").read_text() == "untouched"


@pytest.mark.parametrize("switch_scope", [False, True])
def test_failed_refresh_restores_sources_registration_and_cache(
    system_source, monkeypatch, switch_scope
):
    if switch_scope:
        managed_files.set_config_scope("user")
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    user_before = system_marketplace.read_bundle(cowork._marketplace_root())
    system_before = system_marketplace.read_bundle(system_source)
    state_before = cowork._state_path().read_bytes()
    installed_before = system_marketplace.read_bundle(_installed_source())
    previous_source = cowork._registered_marketplace_source(cowork.discover_cowork_root())
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
    assert cowork._state_path().read_bytes() == state_before
    assert system_marketplace.read_bundle(cowork._marketplace_root()) == user_before
    assert system_marketplace.read_bundle(system_source) == system_before
    assert system_marketplace.read_bundle(_installed_source()) == installed_before
    assert cowork._registered_marketplace_source(cowork.discover_cowork_root()) == previous_source


def test_changed_payload_updates_both_sources_and_installed_version(system_source):
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    before = json.loads((_installed_source() / ".claude-plugin/plugin.json").read_text())["version"]
    assert cowork.configure_cowork(WORKSPACE, [{**SERVERS[0], "name": "changed"}])
    after = json.loads((_installed_source() / ".claude-plugin/plugin.json").read_text())["version"]
    assert after != before
    assert system_marketplace.read_bundle(system_source) == system_marketplace.read_bundle(
        cowork._marketplace_root()
    )
    assert system_marketplace.read_bundle(_installed_source()) == system_marketplace.read_bundle(
        system_source / "plugins/ug"
    )
    assert set(json.loads((_installed_source() / ".mcp.json").read_text())["mcpServers"]) == {
        "changed"
    }


def test_user_scope_preserves_foreign_root_source(system_source):
    system_source.mkdir(parents=True)
    (system_source / "keep").write_text("IT owned")
    managed_files.set_config_scope("user")
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    assert cowork.revert_cowork()
    assert (system_source / "keep").read_text() == "IT owned"


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
    assert not system_source.exists()
    assert not cowork._marketplace_root().exists()
    assert (organization / "keep").read_text() == "older pilot"


def test_failed_rollback_retains_recovery_sources(system_source, tmp_path, monkeypatch):
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    before = system_marketplace.read_bundle(cowork._marketplace_root())
    monkeypatch.setattr(cowork.tempfile, "tempdir", str(tmp_path))
    run = cowork.subprocess_cross_os.run

    def fail_updates(argv, **kwargs):
        if argv[1:3] in (["plugin", "install"], ["plugin", "update"]):
            return SimpleNamespace(returncode=1, stdout="", stderr="update unavailable")
        return run(argv, **kwargs)

    monkeypatch.setattr(cowork.subprocess_cross_os, "run", fail_updates)
    with pytest.raises(RuntimeError, match="rollback failed"):
        cowork.configure_cowork(WORKSPACE, [{**SERVERS[0], "name": "changed"}])
    backups = list(tmp_path.glob("ug-desktop-rollback-*"))
    assert len(backups) == 1
    assert system_marketplace.read_bundle(backups[0] / "user") == before
    assert system_marketplace.read_bundle(backups[0] / "system") == before
    monkeypatch.setattr(cowork.subprocess_cross_os, "run", run)
    assert cowork.revert_cowork()
    assert not system_source.exists()


@pytest.mark.parametrize("operation", ["configure", "revert"])
def test_concurrent_operation_does_not_change_sources_or_state(
    system_source, monkeypatch, operation
):
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    before = system_marketplace.read_bundle(system_source)
    state = cowork._state_path().read_bytes()
    monkeypatch.setattr(cowork, "COWORK_LOCK_TIMEOUT_SECONDS", 0)
    with cowork._locked():
        with pytest.raises(RuntimeError, match="Timed out"):
            if operation == "configure":
                cowork.configure_cowork(WORKSPACE, [])
            else:
                cowork.revert_cowork()
    assert system_marketplace.read_bundle(system_source) == before
    assert system_marketplace.read_bundle(cowork._marketplace_root()) == before
    assert cowork._state_path().read_bytes() == state
