"""System Desktop bundles, migration, and preservation without a native Desktop process."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from ucode import config_io, managed_files
from ucode.agents.desktop import cowork, org_plugins
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
def system_plugin(tmp_path, monkeypatch, request):
    """Use temporary destinations; exercise the real filesystem transaction for each backend."""
    path = tmp_path.resolve() / "system" / "org-plugins" / "ug"
    monkeypatch.setattr(managed_files, "claude_desktop_plugin_path", lambda: path)
    monkeypatch.setattr(managed_files, "current_os", lambda: managed_files.OS(request.param))
    monkeypatch.setattr(managed_files.system_cross_os, "is_admin", lambda: True)
    monkeypatch.setattr(cowork, "claude_desktop_root_directory", lambda: tmp_path / "Desktop")
    monkeypatch.setattr(cowork, "records_for_scope", lambda *args: [])
    monkeypatch.setattr(cowork.shutil, "which", lambda name: str(tmp_path / "bin" / name))
    if request.param == "macos":
        # Run the packaged shell on Linux/macOS without sudo, with only target/OS adapters changed.
        platform = "macos" if os.uname().sysname == "Darwin" else "linux"
        script = managed_files._SUDO_REPLACE_SCRIPT.replace(
            "macos:/Library/Application Support/Claude/org-plugins/ug", f"{platform}:{path}"
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
    return path


def _source(tmp_path):
    source = tmp_path.resolve() / "source"
    (source / ".claude-plugin").mkdir(parents=True)
    (source / ".claude-plugin/plugin.json").write_text('{"name":"ug"}')
    (source / "skills/review").mkdir(parents=True)
    (source / "skills/review/SKILL.md").write_text("Review carefully")
    (source / "skills/review/image.bin").write_bytes(b"\x00\xff\x80")
    executable = source / "skills/review/run.sh"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    return source


def test_install_update_noop_and_revert_preserve_other_plugins(system_plugin, tmp_path):
    source = _source(tmp_path)
    state = config_io.APP_DIR / "test-state.json"
    unrelated = system_plugin.parent / "personal"
    unrelated.mkdir(parents=True)
    (unrelated / "keep").write_text("personal plugin")

    org_plugins.install(source, state)
    first = org_plugins.read_bundle(system_plugin)
    version = json.loads((system_plugin / "version.json").read_text())["version"]
    manifest = json.loads((system_plugin / ".claude-plugin/plugin.json").read_text())
    assert manifest == {"name": "ug", "version": version, "installationPreference": "required"}
    assert (system_plugin / "skills/review/image.bin").read_bytes() == b"\x00\xff\x80"
    if os.name != "nt":
        assert (system_plugin / "skills/review/run.sh").stat().st_mode & 0o777 == 0o755
    timestamp = system_plugin.stat().st_mtime_ns
    org_plugins.install(source, state)
    assert org_plugins.read_bundle(system_plugin) == first
    assert system_plugin.stat().st_mtime_ns == timestamp

    (source / "skills/review/image.bin").unlink()
    (source / "skills/review/SKILL.md").write_text("Updated skill")
    org_plugins.install(source, state)
    assert not (system_plugin / "skills/review/image.bin").exists()
    assert (system_plugin / "skills/review/SKILL.md").read_text() == "Updated skill"
    assert json.loads((system_plugin / "version.json").read_text())["version"] != version
    assert org_plugins.remove(state)
    assert not system_plugin.exists()
    assert not org_plugins.remove(state)
    assert (unrelated / "keep").read_text() == "personal plugin"


def test_foreign_or_edited_installation_is_preserved(system_plugin, tmp_path):
    source = _source(tmp_path)
    state = config_io.APP_DIR / "test-state.json"
    system_plugin.mkdir(parents=True)
    custom = system_plugin / "it-owned.txt"
    custom.write_text("IT installation")
    with pytest.raises(RuntimeError, match="not this user's UG installation"):
        org_plugins.install(source, state)
    assert not org_plugins.remove(state)
    assert custom.read_text() == "IT installation"
    custom.unlink()
    system_plugin.rmdir()
    org_plugins.install(source, state)
    (system_plugin / "skills/review/SKILL.md").write_text("External edit")
    before = state.read_bytes()
    with pytest.raises(RuntimeError, match="edited outside UG"):
        org_plugins.remove(state)
    assert (system_plugin / "skills/review/SKILL.md").read_text() == "External edit"
    assert state.read_bytes() == before


def test_system_configure_needs_no_cowork_session_and_preserves_payload(system_plugin, tmp_path):
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    mcp = json.loads((system_plugin / ".mcp.json").read_text())
    assert mcp == cowork._cowork_mcp_config(SERVERS, **WORKSPACE)
    hooks = json.loads((system_plugin / "hooks/hooks.json").read_text())
    assert hooks == cowork._connector_hooks(mcp, **WORKSPACE, use_pat=False)
    assert (system_plugin / "skills" / cowork.CONNECTOR_SKILL_NAME / "SKILL.md").is_file()
    assert not cowork._marketplace_root().exists()
    assert not (tmp_path / "Desktop").exists()
    assert cowork.revert_cowork()
    assert not system_plugin.exists()
    assert not cowork._state_path().exists()


def test_migration_removes_registration_only_after_system_install(
    system_plugin, tmp_path, monkeypatch
):
    old_root = tmp_path / "old-cowork"
    config_io.atomic_write_json(cowork._state_path(), {"version": 1, "cowork_root": str(old_root)})
    cowork._marketplace_root().mkdir()
    calls = []

    def run(argv, **kwargs):
        assert (system_plugin / ".mcp.json").is_file()
        assert kwargs["env"]["CLAUDE_CONFIG_DIR"] == str(old_root)
        calls.append(argv[1:])
        return SimpleNamespace(returncode=0, stdout='{"outcome":"ok"}', stderr="")

    monkeypatch.setattr(cowork.subprocess_cross_os, "run", run)
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    assert calls == [
        ["plugin", "uninstall", cowork.PLUGIN_ID, "--cowork", "--json"],
        ["plugin", "uninstall", cowork.LEGACY_PLUGIN_ID, "--cowork", "--json"],
        ["plugin", "marketplace", "remove", cowork.MARKETPLACE_NAME, "--cowork", "--json"],
    ]
    assert "cowork_root" not in config_io.read_json_safe(cowork._state_path())
    assert not cowork._marketplace_root().exists()


def test_install_failure_preserves_user_registration_and_state(
    system_plugin, tmp_path, monkeypatch
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
    assert not system_plugin.exists()


@pytest.mark.parametrize("opt_out", ["user", "environment"])
def test_opt_out_rejects_duplicate_system_plugin(system_plugin, monkeypatch, opt_out):
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    before = org_plugins.read_bundle(system_plugin)
    if opt_out == "user":
        managed_files.set_config_scope("user")
    else:
        monkeypatch.setenv("UG_SKIP_OS_SETTINGS", "1")
    with pytest.raises(RuntimeError, match="system Desktop plugin already exists"):
        cowork.configure_cowork(WORKSPACE, SERVERS)
    assert org_plugins.read_bundle(system_plugin) == before
    assert not cowork._marketplace_root().exists()


def test_dry_run_leaves_no_state_or_bundle(system_plugin):
    config_io.set_dry_run(True)
    try:
        assert not cowork.configure_cowork(WORKSPACE, SERVERS)
        assert not cowork.revert_cowork()
        assert not system_plugin.exists()
        assert not cowork._state_path().exists()
    finally:
        config_io.set_dry_run(False)


def test_pending_installation_can_be_reverted(system_plugin):
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    state = config_io.read_json_safe(cowork._state_path())
    state["pending_system_digest"] = state.pop("system_digest")
    config_io.atomic_write_json(cowork._state_path(), state)
    assert cowork.revert_cowork()
    assert not system_plugin.exists()


def test_failed_update_restores_previous_bundle(system_plugin, tmp_path, monkeypatch):
    source = _source(tmp_path)
    state = config_io.APP_DIR / "test-state.json"
    org_plugins.install(source, state)
    before = org_plugins.read_bundle(system_plugin)
    previous_state = state.read_bytes()
    (source / "skills/review/SKILL.md").write_text("Updated skill")
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
        org_plugins.install(source, state)
    assert org_plugins.read_bundle(system_plugin) == before
    assert state.read_bytes() == previous_state
    assert not list(system_plugin.parent.glob(".ug-install*"))


def test_concurrent_edit_is_not_overwritten(system_plugin, tmp_path):
    source = _source(tmp_path)
    state = config_io.APP_DIR / "test-state.json"
    org_plugins.install(source, state)
    previous = org_plugins.read_bundle(system_plugin)
    (system_plugin / "skills/review/SKILL.md").write_text("Concurrent edit")
    with pytest.raises(RuntimeError):
        org_plugins._update_bundle(system_plugin, None, previous)
    assert (system_plugin / "skills/review/SKILL.md").read_text() == "Concurrent edit"


def test_failed_registration_cleanup_can_be_retried(system_plugin, tmp_path, monkeypatch):
    old = tmp_path / "old"
    config_io.atomic_write_json(cowork._state_path(), {"version": 1, "cowork_root": str(old)})

    def failed_cli(*args, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="plugin command unavailable")

    monkeypatch.setattr(cowork.subprocess_cross_os, "run", failed_cli)
    with pytest.raises(RuntimeError, match="plugin command unavailable"):
        cowork.configure_cowork(WORKSPACE, SERVERS)
    pending = config_io.read_json_safe(cowork._state_path())
    assert pending["cowork_root"] == str(old)
    assert pending["system_digest"] == org_plugins.bundle_digest(
        org_plugins.read_bundle(system_plugin)
    )
    monkeypatch.setattr(
        cowork.subprocess_cross_os,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout='{"outcome":"ok"}', stderr=""),
    )
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    assert "cowork_root" not in config_io.read_json_safe(cowork._state_path())


@pytest.mark.parametrize("opt_out", ["user", "environment"])
def test_opt_out_uses_existing_user_installer(system_plugin, tmp_path, monkeypatch, opt_out):
    if opt_out == "user":
        managed_files.set_config_scope("user")
    else:
        monkeypatch.setenv("UG_SKIP_OS_SETTINGS", "1")
    root = tmp_path / "Desktop/local-agent-mode-sessions/account/org"
    root.mkdir(parents=True)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout='{"outcome":"ok"}', stderr="")

    monkeypatch.setattr(cowork.subprocess_cross_os, "run", run)
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    assert any(argv[1:4] == ["plugin", "install", cowork.PLUGIN_ID] for argv in calls)
    assert config_io.read_json_safe(cowork._state_path())["cowork_root"] == str(root)
    assert not system_plugin.exists()


def test_removing_all_contents_updates_existing_system_plugin(system_plugin):
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    assert cowork.configure_cowork(WORKSPACE, [])
    assert (system_plugin / ".claude-plugin/plugin.json").is_file()
    assert not (system_plugin / ".mcp.json").exists()
    assert not (system_plugin / "hooks").exists()
    assert not (system_plugin / "skills").exists()


def test_downloaded_skills_follow_workspace_selection(system_plugin, tmp_path, monkeypatch):
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
    assert (system_plugin / "skills/selected/SKILL.md").read_text() == "selected"
    assert (system_plugin / "skills/selected/asset.bin").read_bytes() == b"\xff\x00"
    assert not (system_plugin / "skills/foreign").exists()
    assert cowork.configure_cowork({**WORKSPACE, "workspace": "https://other.example"}, [])
    assert (system_plugin / "skills/foreign/SKILL.md").read_text() == "foreign"
    assert not (system_plugin / "skills/selected").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX temporary-directory symlink")
def test_system_configure_accepts_os_temporary_directory_alias(
    system_plugin, tmp_path, monkeypatch
):
    physical = tmp_path / "physical-temp"
    physical.mkdir()
    alias = tmp_path / "temp-alias"
    alias.symlink_to(physical, target_is_directory=True)
    monkeypatch.setattr(cowork.tempfile, "tempdir", str(alias))
    assert cowork.configure_cowork(WORKSPACE, SERVERS)
    assert (system_plugin / ".mcp.json").is_file()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink input")
def test_system_plugin_link_is_preserved(system_plugin, tmp_path):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "keep").write_text("untouched")
    system_plugin.parent.mkdir(parents=True)
    system_plugin.symlink_to(foreign, target_is_directory=True)
    with pytest.raises(RuntimeError, match="linked Desktop plugin path"):
        cowork.configure_cowork(WORKSPACE, SERVERS)
    assert system_plugin.is_symlink()
    assert (foreign / "keep").read_text() == "untouched"
