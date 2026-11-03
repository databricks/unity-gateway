"""System Desktop sources, source switching, and scoped cleanup without a native Desktop process."""

import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from ucode import config_io, managed_files
from ucode.agents.desktop import cowork
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
    """Use temporary destinations; exercise the real directory copy for each backend."""
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
        */new)
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


def test_install_update_and_revert_preserve_other_plugins(system_source, tmp_path):
    source = _source(tmp_path)
    unrelated = system_source.parent / "personal"
    unrelated.mkdir(parents=True)
    (unrelated / "keep").write_text("personal plugin")

    managed_files.replace_privileged_bundle(system_source, source)
    first = cowork._marketplace_files(system_source)
    manifest = json.loads((system_source / "plugins/ug/.claude-plugin/plugin.json").read_text())
    assert manifest == {"name": "ug", "version": "1.0.0"}
    assert not (system_source / "version.json").exists()
    assert cowork._marketplace_files(source) == first
    assert (system_source / "plugins/ug/skills/review/image.bin").read_bytes() == b"\x00\xff\x80"
    if os.name != "nt":
        assert (system_source / "plugins/ug/skills/review/run.sh").stat().st_mode & 0o777 == 0o755
    managed_files.replace_privileged_bundle(system_source, source)
    assert cowork._marketplace_files(system_source) == first

    (source / "plugins/ug/skills/review/image.bin").unlink()
    (source / "plugins/ug/skills/review/SKILL.md").write_text("Updated skill")
    managed_files.replace_privileged_bundle(system_source, source)
    assert not (system_source / "plugins/ug/skills/review/image.bin").exists()
    assert (system_source / "plugins/ug/skills/review/SKILL.md").read_text() == "Updated skill"
    assert cowork._marketplace_files(source) == cowork._marketplace_files(system_source)
    managed_files.replace_privileged_bundle(system_source, None)
    assert not system_source.exists()
    managed_files.replace_privileged_bundle(system_source, None)
    assert not system_source.exists()
    assert (unrelated / "keep").read_text() == "personal plugin"


def test_ug_directory_is_replaced_without_ownership_hashes(system_source, tmp_path):
    source = _source(tmp_path)
    system_source.mkdir(parents=True)
    custom = system_source / "manual.txt"
    custom.write_text("Manual content in UG's directory")
    managed_files.replace_privileged_bundle(system_source, source)
    assert not custom.exists()
    skill = system_source / "plugins/ug/skills/review/SKILL.md"
    skill.write_text("External edit")
    managed_files.replace_privileged_bundle(system_source, source)
    assert skill.read_text() == "Review carefully"
    skill.write_text("Another edit")
    managed_files.replace_privileged_bundle(system_source, None)
    assert not system_source.exists()


def test_global_configure_writes_both_sources_and_one_user_install(system_source):
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    assert cowork._marketplace_files(system_source) == cowork._marketplace_files(
        cowork._marketplace_root()
    )
    plugin = system_source / "plugins/ug"
    mcp = json.loads((plugin / ".mcp.json").read_text())
    assert mcp == cowork._cowork_mcp_config(SERVERS, **WORKSPACE)
    hooks = json.loads((plugin / "hooks/hooks.json").read_text())
    assert hooks == cowork._connector_hooks(mcp, **WORKSPACE, use_pat=False)
    assert cowork._registered_marketplace_source(cowork.discover_cowork_root()) == system_source
    assert cowork._marketplace_files(_installed_source()) == cowork._marketplace_files(plugin)
    assert not (plugin / "version.json").exists()
    assert "installationPreference" not in json.loads(
        (plugin / ".claude-plugin/plugin.json").read_text()
    )
    before = cowork._marketplace_files(system_source)
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    assert cowork._marketplace_files(system_source) == before
    _installed_source()
    assert cowork.revert_cowork()
    assert not system_source.exists()
    assert not cowork._marketplace_root().exists()
    assert not cowork._state_path().exists()


@pytest.mark.parametrize("scope", ["global", "user"])
def test_first_configure_skips_uninitialized_cowork(system_source, tmp_path, scope):
    managed_files.set_config_scope(scope)
    shutil.rmtree(tmp_path / "Desktop")
    assert not cowork.configure_cowork(WORKSPACE, SERVERS)
    assert not system_source.exists()
    assert not cowork._marketplace_root().exists()
    assert not cowork._state_path().exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell worker")
@pytest.mark.parametrize("system_source", ["macos"], indirect=True)
@pytest.mark.parametrize("entry_type", ["symlink", "hardlink", "fifo"])
def test_macos_root_copy_rejects_linked_and_special_source_files(
    system_source, tmp_path, entry_type
):
    source = _source(tmp_path)
    managed_files.replace_privileged_bundle(system_source, source)
    original = cowork._marketplace_files(system_source)
    skill = source / "plugins/ug/skills/review/SKILL.md"
    extra = skill.with_name("extra")
    if entry_type == "symlink":
        extra.symlink_to(skill)
    elif entry_type == "hardlink":
        extra.hardlink_to(skill)
    else:
        os.mkfifo(extra)

    with pytest.raises(RuntimeError, match="Desktop plugin source must contain only caller-owned"):
        managed_files.replace_privileged_bundle(system_source, source)

    assert cowork._marketplace_files(system_source) == original
    assert not list(system_source.parent.glob(".ug-install*"))


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
        assert cowork._marketplace_files(cowork._marketplace_root()) == cowork._marketplace_files(
            system_source
        )
    assert cowork.revert_cowork()
    assert config_io.read_json_safe(registry)["plugins"] == {
        "personal@other": [{"installPath": "untouched"}]
    }


def test_root_write_failure_leaves_registration_and_state_for_retry(
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
    assert not marker.exists()
    assert (cowork._marketplace_root() / "plugins/ug/.mcp.json").is_file()
    registry = cowork.discover_cowork_root() / "cowork_plugins/known_marketplaces.json"
    assert not registry.exists()
    assert not system_source.exists()


@pytest.mark.parametrize("opt_out", ["user", "environment"])
def test_opt_out_keeps_root_source_untouched(system_source, monkeypatch, opt_out):
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    before = cowork._marketplace_files(system_source)
    if opt_out == "user":
        managed_files.set_config_scope("user")
    else:
        monkeypatch.setenv("UG_SKIP_OS_SETTINGS", "1")
    assert cowork.configure_cowork(WORKSPACE, [])
    assert cowork._marketplace_files(system_source) == before
    assert (
        cowork._registered_marketplace_source(cowork.discover_cowork_root())
        == cowork._marketplace_root()
    )
    assert not (_installed_source() / ".mcp.json").exists()
    assert cowork.revert_cowork()
    assert cowork._marketplace_files(system_source) == before
    assert not cowork._state_path().exists()
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


def test_configure_records_selected_source_for_revert(system_source):
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    assert config_io.read_json_safe(cowork._state_path()) == {
        "version": 1,
        "cowork_root": str(cowork.discover_cowork_root()),
        "marketplace_root": str(system_source),
    }
    assert cowork.revert_cowork()
    assert not system_source.exists()


def test_failed_root_copy_can_be_retried(system_source, tmp_path, monkeypatch):
    source = _source(tmp_path)
    managed_files.replace_privileged_bundle(system_source, source)
    (source / "plugins/ug/skills/review/SKILL.md").write_text("Updated skill")
    copy = shutil.copytree
    failed = False

    def fail_copy(src, dst, *args, **kwargs):
        nonlocal failed
        if not failed and Path(dst) == system_source:
            failed = True
            raise PermissionError("simulated copy failure")
        return copy(src, dst, *args, **kwargs)

    if managed_files.current_os() is managed_files.OS.WINDOWS:
        monkeypatch.setattr(shutil, "copytree", fail_copy)
    else:
        monkeypatch.setenv("UG_TEST_BUNDLE_FAIL", "rename")
    with pytest.raises((RuntimeError, PermissionError)):
        managed_files.replace_privileged_bundle(system_source, source)
    monkeypatch.delenv("UG_TEST_BUNDLE_FAIL", raising=False)
    managed_files.replace_privileged_bundle(system_source, source)
    assert cowork._marketplace_files(system_source) == cowork._marketplace_files(source)
    assert not list(system_source.parent.glob(".ug-install*"))


def test_root_copy_rejects_unexpected_target(system_source, tmp_path):
    other = system_source.parent / "other-plugin"
    other.mkdir(parents=True)
    (other / "keep").write_text("Unrelated plugin")
    with pytest.raises(RuntimeError, match="unexpected privileged bundle target"):
        managed_files.replace_privileged_bundle(other, _source(tmp_path))
    assert (other / "keep").read_text() == "Unrelated plugin"


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
    assert not system_source.exists()
    assert not cowork._marketplace_root().exists()
    monkeypatch.setattr(cowork, "_remove_plugin_registration", remove)
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    assert config_io.read_json_safe(cowork._state_path()) == {
        "version": 1,
        "cowork_root": str(cowork.discover_cowork_root()),
        "marketplace_root": str(system_source),
    }
    assert config_io.read_json_safe(old / "cowork_plugins/installed_plugins.json")["plugins"] == {}


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
def test_failed_refresh_retains_generated_sources_for_retry(
    system_source, monkeypatch, switch_scope
):
    if switch_scope:
        managed_files.set_config_scope("user")
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    user_before = cowork._marketplace_files(cowork._marketplace_root())
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
    generated = cowork._marketplace_files(cowork._marketplace_root())
    assert generated != user_before
    assert cowork._marketplace_files(system_source) == generated
    state = config_io.read_json_safe(cowork._state_path())
    assert state["marketplace_root"] == str(system_source)
    assert "system_digest" not in state
    assert "pending_system_digest" not in state
    assert cowork.configure_cowork(WORKSPACE, [{**SERVERS[0], "name": "changed"}])
    assert cowork._marketplace_files(_installed_source()) == cowork._marketplace_files(
        system_source / "plugins/ug"
    )
    assert cowork._registered_marketplace_source(cowork.discover_cowork_root()) == system_source


def test_changed_payload_updates_both_sources_and_installed_version(system_source):
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    before = json.loads((_installed_source() / ".claude-plugin/plugin.json").read_text())["version"]
    assert cowork.configure_cowork(WORKSPACE, [{**SERVERS[0], "name": "changed"}])
    after = json.loads((_installed_source() / ".claude-plugin/plugin.json").read_text())["version"]
    assert after != before
    assert cowork._marketplace_files(system_source) == cowork._marketplace_files(
        cowork._marketplace_root()
    )
    assert cowork._marketplace_files(_installed_source()) == cowork._marketplace_files(
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


def test_failed_refresh_retains_owned_sources_for_revert(system_source, monkeypatch):
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    before = cowork._marketplace_files(cowork._marketplace_root())
    run = cowork.subprocess_cross_os.run

    def fail_updates(argv, **kwargs):
        if argv[1:3] in (["plugin", "install"], ["plugin", "update"]):
            return SimpleNamespace(returncode=1, stdout="", stderr="update unavailable")
        return run(argv, **kwargs)

    monkeypatch.setattr(cowork.subprocess_cross_os, "run", fail_updates)
    with pytest.raises(RuntimeError, match="update unavailable"):
        cowork.configure_cowork(WORKSPACE, [{**SERVERS[0], "name": "changed"}])
    generated = cowork._marketplace_files(system_source)
    assert generated != before
    assert generated == cowork._marketplace_files(cowork._marketplace_root())
    monkeypatch.setattr(cowork.subprocess_cross_os, "run", run)
    assert cowork.revert_cowork()
    assert not system_source.exists()
    assert not cowork._marketplace_root().exists()
    assert not cowork._state_path().exists()
