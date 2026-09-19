"""Unit coverage for integration evidence collection, without launching an agent."""

import hashlib
import tracemalloc

import pytest

from tests.integration.utils.managed import codex_state_snapshot


@pytest.fixture
def snapshot_home(tmp_path):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".ucode").mkdir()
    return home


def test_codex_snapshot_ignores_only_bootstrap_links_and_control_plane_cache(snapshot_home):
    home = snapshot_home
    config = home / ".codex/config.toml"
    config.write_text('model = "example"\n')
    before = codex_state_snapshot(home)
    bootstrap = home / ".codex/tmp/arg0/random-launch"
    bootstrap.mkdir(parents=True)
    (bootstrap / "apply_patch").symlink_to("/unreadable/agent-binary")
    (bootstrap / ".lock").touch()
    (home / ".ucode/managed-config.json").write_text('{"fetched_at": 1}')
    assert codex_state_snapshot(home) == before

    (bootstrap / "apply_patch").unlink()
    (bootstrap / "new-helper").symlink_to("/another/agent-binary")
    (home / ".ucode/managed-config.json").write_text('{"fetched_at": 2}')
    assert codex_state_snapshot(home) == before

    # A blanket exclusion of .codex/tmp would hide unrelated persistent changes.
    (home / ".codex/tmp/other-file").write_text("must remain covered")
    assert ".codex/tmp/other-file" in codex_state_snapshot(home)


@pytest.mark.parametrize("name", [".ucode/state.json", ".codex/config.toml"])
def test_codex_snapshot_detects_added_changed_and_removed_persistent_files(snapshot_home, name):
    path = snapshot_home / name
    empty = codex_state_snapshot(snapshot_home)
    path.write_text("original")
    before = codex_state_snapshot(snapshot_home)
    assert before != empty
    assert before[name] == ("sha256", hashlib.sha256(b"original").hexdigest())
    path.write_text("modified")
    assert codex_state_snapshot(snapshot_home) != before
    path.unlink()
    assert codex_state_snapshot(snapshot_home) == empty


def test_codex_snapshot_records_links_without_following_files_or_directories(
    snapshot_home, tmp_path
):
    external = tmp_path / "external"
    external.mkdir()
    target = external / "large-binary"
    target.write_bytes(b"original binary")
    links = snapshot_home / ".codex"
    (links / "file-link").symlink_to(target)
    (links / "directory-link").symlink_to(external, target_is_directory=True)
    (links / "broken-link").symlink_to("missing")
    (links / "cycle").symlink_to(links, target_is_directory=True)
    before = codex_state_snapshot(snapshot_home)
    assert len(before) == 4
    assert before[".codex/file-link"] == ("symlink", str(target))
    assert before[".codex/directory-link"] == ("symlink", str(external))
    assert before[".codex/broken-link"] == ("symlink", "missing")
    target.write_bytes(b"changed outside the test home")
    assert codex_state_snapshot(snapshot_home) == before
    (links / "broken-link").unlink()
    (links / "broken-link").symlink_to("different-target")
    assert codex_state_snapshot(snapshot_home) != before


def test_codex_snapshot_hashes_large_files_with_bounded_memory(snapshot_home):
    path = snapshot_home / ".codex/large-file"
    with path.open("wb") as output:
        output.truncate(16 * 1024 * 1024)
    tracemalloc.start()
    try:
        snapshot = codex_state_snapshot(snapshot_home)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 1024 * 1024, f"Snapshot allocated {peak} bytes for a 16 MiB file"
    assert len(snapshot[".codex/large-file"][1]) == 64


def test_codex_snapshot_handles_a_fresh_home(tmp_path):
    assert codex_state_snapshot(tmp_path / "not-created") == {}
