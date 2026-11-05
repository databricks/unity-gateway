"""Root organization-plugin publication for macOS MDM, using the packaged shell worker."""

import json
import os

import pytest

from tests.test_desktop_marketplace import desktop_environment
from ucode import managed_files
from ucode.agents.desktop import cowork

_REAL_SUDO_REPLACE = managed_files._sudo_replace


@pytest.fixture
def system_source(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("macOS MDM publication uses a POSIX shell")
    run = managed_files.subprocess_cross_os.run
    path = desktop_environment(tmp_path, monkeypatch, "macos").parent / "org-plugins/ug"
    monkeypatch.setattr(managed_files, "claude_desktop_org_plugin_path", lambda: path)
    native_cli = managed_files.subprocess_cross_os.run

    def run_command(argv, **kwargs):
        if argv[1:2] == ["plugin"]:
            return native_cli(argv, **kwargs)
        return run(argv, **kwargs)

    monkeypatch.setattr(managed_files.subprocess_cross_os, "run", run_command)
    # Run the packaged shell on Linux/macOS without sudo, with only target/OS adapters changed.
    platform = "macos" if os.uname().sysname == "Darwin" else "linux"
    script = managed_files._SUDO_REPLACE_SCRIPT.replace(
        "macos:/Library/Application Support/Claude/org-plugins/ug",
        f"{platform}:{path}",
    )
    if platform == "linux":
        script = script.replace("/usr/bin/stat -f %u", "stat -c %u")
    files = {
        managed_files.CLAUDE_DESKTOP_POLICY_PATH: tmp_path / "machine/policy.plist",
        managed_files.CLAUDE_DESKTOP_MDM_ROOT / "ug": tmp_path / "machine/mdm/ug",
        managed_files.CLAUDE_DESKTOP_MDM_ROOT / "otel-headers.sh": tmp_path
        / "machine/mdm/otel-headers.sh",
    }
    for original, temporary in files.items():
        script = script.replace(f"macos:{original}", f"{platform}:{temporary}")
    monkeypatch.setitem(
        managed_files._SUDO_REPLACE_TARGETS, managed_files.OS.MACOS, frozenset(files.values())
    )
    monkeypatch.setattr(managed_files, "_sudo_command", lambda *args: list(args))
    monkeypatch.setattr(managed_files, "_sudo_replace", _REAL_SUDO_REPLACE)
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
        lambda mode, *args, **kwargs: [
            "/usr/bin/env",
            f"SUDO_UID={os.getuid()}",
            "/bin/sh",
            "-c",
            script,
            "bundle-test",
            mode,
            platform,
            *args,
        ],
    )
    return path


def _source(tmp_path):
    source = tmp_path / "organization-plugin"
    (source / ".claude-plugin").mkdir(parents=True)
    (source / ".claude-plugin/plugin.json").write_text(
        json.dumps({"name": "ug", "version": "1.0.0", "installationPreference": "required"})
    )
    (source / "version.json").write_text(json.dumps({"version": "1.0.0"}))
    skill = source / "skills/review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("Review carefully")
    (skill / "image.bin").write_bytes(b"\x00\xff\x80")
    script = skill / "run.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    return source


def test_publish_update_and_removal_preserve_other_plugins(system_source, tmp_path):
    source = _source(tmp_path)
    unrelated = system_source.parent / "personal"
    unrelated.mkdir(parents=True)
    (unrelated / "keep").write_text("personal plugin")

    managed_files.replace_privileged_bundle(system_source, source)
    first = cowork._marketplace_files(system_source)
    manifest = json.loads((system_source / ".claude-plugin/plugin.json").read_text())
    assert manifest == {"name": "ug", "version": "1.0.0", "installationPreference": "required"}
    assert json.loads((system_source / "version.json").read_text()) == {"version": "1.0.0"}
    assert cowork._marketplace_files(source) == first
    assert (system_source / "skills/review/image.bin").read_bytes() == b"\x00\xff\x80"
    assert (system_source / "skills/review/run.sh").stat().st_mode & 0o777 == 0o755
    managed_files.replace_privileged_bundle(system_source, source)
    assert cowork._marketplace_files(system_source) == first

    (source / "skills/review/image.bin").unlink()
    (source / "skills/review/SKILL.md").write_text("Updated skill")
    managed_files.replace_privileged_bundle(system_source, source)
    assert not (system_source / "skills/review/image.bin").exists()
    assert (system_source / "skills/review/SKILL.md").read_text() == "Updated skill"
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
    skill = system_source / "skills/review/SKILL.md"
    skill.write_text("External edit")
    managed_files.replace_privileged_bundle(system_source, source)
    assert skill.read_text() == "Review carefully"
    skill.write_text("Another edit")
    managed_files.replace_privileged_bundle(system_source, None)
    assert not system_source.exists()


@pytest.mark.parametrize("entry_type", ["symlink", "hardlink", "fifo"])
def test_macos_root_copy_rejects_linked_and_special_source_files(
    system_source, tmp_path, entry_type
):
    source = _source(tmp_path)
    managed_files.replace_privileged_bundle(system_source, source)
    original = cowork._marketplace_files(system_source)
    skill = source / "skills/review/SKILL.md"
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


def test_macos_publication_accepts_non_root_caller(system_source, tmp_path, monkeypatch):
    monkeypatch.setattr(managed_files.system_cross_os, "is_admin", lambda: False)
    monkeypatch.setattr(managed_files, "_running_as_root", lambda: False)
    source = _source(tmp_path)
    managed_files.replace_privileged_bundle(system_source, source)
    assert cowork._marketplace_files(system_source) == cowork._marketplace_files(source)
    managed_files.replace_privileged_bundle(system_source, None)
    assert not system_source.exists()


def test_macos_worker_rejects_another_users_bundle(system_source, tmp_path, monkeypatch):
    command = managed_files._sudo_replace_command

    def another_user_command(mode):
        argv = command(mode)
        argv[1] = f"SUDO_UID={os.getuid() + 1}"
        return argv

    monkeypatch.setattr(managed_files, "_sudo_replace_command", another_user_command)
    with pytest.raises(RuntimeError, match="Invalid Desktop plugin source owner"):
        managed_files.replace_privileged_bundle(system_source, _source(tmp_path))
    assert not system_source.exists()


def test_failed_root_copy_can_be_retried(system_source, tmp_path, monkeypatch):
    source = _source(tmp_path)
    managed_files.replace_privileged_bundle(system_source, source)
    (source / "skills/review/SKILL.md").write_text("Updated skill")
    before = cowork._marketplace_files(system_source)
    monkeypatch.setenv("UG_TEST_BUNDLE_FAIL", "rename")
    with pytest.raises(RuntimeError):
        managed_files.replace_privileged_bundle(system_source, source)
    assert cowork._marketplace_files(system_source) == before
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


def test_root_copy_preserves_linked_system_destination(system_source, tmp_path):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "keep").write_text("untouched")
    system_source.parent.mkdir(parents=True, exist_ok=True)
    system_source.symlink_to(foreign, target_is_directory=True)
    with pytest.raises(RuntimeError, match="linked Desktop plugin path"):
        managed_files.replace_privileged_bundle(system_source, _source(tmp_path))
    assert system_source.is_symlink()
    assert (foreign / "keep").read_text() == "untouched"


@pytest.mark.parametrize("manifest", ["version.json", ".claude-plugin/plugin.json"])
def test_missing_manifest_reports_path_and_preserves_published_bundle(
    system_source, tmp_path, manifest
):
    source = _source(tmp_path)
    managed_files.replace_privileged_bundle(system_source, source)
    before = cowork._marketplace_files(system_source)
    (source / manifest).unlink()
    with pytest.raises(RuntimeError, match="missing manifest: " + manifest):
        managed_files.replace_privileged_bundle(system_source, source)
    assert cowork._marketplace_files(system_source) == before
