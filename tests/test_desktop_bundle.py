"""Root marketplace publication for macOS MDM, using the packaged shell worker."""

import json
import os
from unittest.mock import Mock

import pytest

from tests.test_desktop_marketplace import _source, desktop_environment
from ucode import managed_files
from ucode.agents.desktop import cowork


@pytest.fixture
def system_source(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("macOS MDM publication uses a POSIX shell")
    path = desktop_environment(tmp_path, monkeypatch, "macos")
    monkeypatch.setattr(managed_files, "claude_desktop_marketplace_path", lambda: path)
    # Run the packaged shell on Linux/macOS without sudo, with only target/OS adapters changed.
    platform = "macos" if os.uname().sysname == "Darwin" else "linux"
    script = managed_files._SUDO_REPLACE_SCRIPT.replace(
        "macos:/Library/Application Support/UnityGateway/claude-cowork-marketplace",
        f"{platform}:{path}",
    )
    if platform == "linux":
        script = script.replace("/usr/bin/stat -f %u", "stat -c %u")
    script = script.replace("root_uid=0", f"root_uid={os.getuid()}")
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
    return path


def test_publish_update_and_removal_preserve_other_plugins(system_source, tmp_path):
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


def test_macos_publication_requires_root_without_starting_sudo(system_source, monkeypatch):
    monkeypatch.setattr(managed_files.system_cross_os, "is_admin", lambda: False)
    worker = Mock(side_effect=AssertionError("MDM publication must not start sudo"))
    monkeypatch.setattr(managed_files, "_session_worker", worker)
    with pytest.raises(RuntimeError, match="as root"):
        managed_files.replace_privileged_bundle(system_source, None)
    worker.assert_not_called()


def test_macos_worker_rejects_non_root_bundle_requests(system_source, monkeypatch):
    command = managed_files._sudo_replace_command

    def non_root_command(mode):
        argv = command(mode)
        argv[1] = f"SUDO_UID={os.getuid() + 1}"
        return argv

    monkeypatch.setattr(managed_files, "_sudo_replace_command", non_root_command)
    with pytest.raises(RuntimeError, match="requires a root caller"):
        managed_files.replace_privileged_bundle(system_source, None)
    assert not system_source.exists()


def test_failed_root_copy_can_be_retried(system_source, tmp_path, monkeypatch):
    source = _source(tmp_path)
    managed_files.replace_privileged_bundle(system_source, source)
    (source / "plugins/ug/skills/review/SKILL.md").write_text("Updated skill")
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


@pytest.mark.parametrize(
    "manifest", [".claude-plugin/marketplace.json", "plugins/ug/.claude-plugin/plugin.json"]
)
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
