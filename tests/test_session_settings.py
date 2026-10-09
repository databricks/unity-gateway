"""General session settings isolation, validation, atomicity, and concurrent writers."""

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from ucode import config_io
from ucode.session_settings import (
    create_session_settings,
    read_session_settings,
    replace_session_settings,
    update_session_settings,
)


def test_updates_merge_nested_settings_and_isolate_sessions():
    original = {"env": {"FIRST": "one", "KEEP": "yes"}, "other": {"keep": [1, 2]}}
    first = create_session_settings(original)
    second = create_session_settings(original)
    updated = update_session_settings(first, {"env": {"FIRST": "two"}, "other": {"new": True}})
    assert updated == {
        "env": {"FIRST": "two", "KEEP": "yes"},
        "other": {"keep": [1, 2], "new": True},
    }
    assert read_session_settings(first) == updated
    assert read_session_settings(second) == original
    assert original["env"]["FIRST"] == "one"
    assert first.parent != second.parent


def test_explicit_removal_preserves_siblings_and_updates_win():
    path = create_session_settings({"env": {"DROP": "old", "KEEP": "yes"}, "model": "old"})
    update_session_settings(path, {"model": "new"}, remove_paths=[["env", "DROP"], ["model"]])
    assert read_session_settings(path) == {"env": {"KEEP": "yes"}, "model": "new"}
    replace_session_settings(path, {"env": {"OTHER": "value"}})
    assert read_session_settings(path) == {"env": {"OTHER": "value"}}


@pytest.mark.parametrize(
    "settings",
    [
        [],
        {"env": []},
        {"env": {"NAME": 42}},
        {1: "value"},
        {"nested": {1: "value"}},
        {"value": float("nan")},
        {"value": object()},
    ],
)
def test_invalid_updates_do_not_change_existing_state(settings):
    path = create_session_settings({"env": {"KEEP": "yes"}})
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="Session settings must be a JSON object"):
        update_session_settings(path, settings)
    assert path.read_bytes() == before


@pytest.mark.parametrize("raw", ["not-json", "[]", '{"env":{"NAME":42}}'])
def test_corrupt_state_is_not_silently_replaced(tmp_path, raw):
    path = tmp_path / "settings.json"
    path.write_text(raw)
    with pytest.raises(RuntimeError, match="missing or invalid"):
        update_session_settings(path, {"model": "new"})
    assert path.read_text() == raw


def test_missing_file_is_not_recreated(tmp_path):
    path = tmp_path / "missing.json"
    with pytest.raises(RuntimeError, match="missing or invalid"):
        update_session_settings(path, {})
    assert not path.exists()


@pytest.mark.parametrize("filename", ["", "../escape.json", "nested/file.json", ".", ".."])
def test_session_filename_cannot_escape_private_directory(filename):
    with pytest.raises(ValueError, match="single filename"):
        create_session_settings(filename=filename)


def test_failed_atomic_replace_preserves_snapshot_and_cleans_temp_files(monkeypatch):
    path = create_session_settings({"env": {"KEEP": "yes"}})
    original = path.read_bytes()

    def fail_replace(*_args):
        raise OSError("disk failure")

    monkeypatch.setattr(config_io.os, "replace", fail_replace)
    with pytest.raises(RuntimeError, match="Failed to write file"):
        update_session_settings(path, {"env": {"KEEP": "changed"}})
    assert path.read_bytes() == original
    assert not list(path.parent.glob("*.tmp"))


def test_cross_process_updates_preserve_all_writers():
    path = create_session_settings({"env": {"KEEP": "yes"}})
    script = """
import sys
from pathlib import Path
from ucode.session_settings import update_session_settings
for index in range(20):
    update_session_settings(Path(sys.argv[1]), {"env": {f"WORKER_{sys.argv[2]}_{index}": "value"}})
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(path), str(worker)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for worker in range(4)
    ]
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            assert process.returncode == 0, stdout + stderr
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
    assert read_session_settings(path)["env"] == {
        "KEEP": "yes",
        **{f"WORKER_{worker}_{index}": "value" for worker in range(4) for index in range(20)},
    }


def test_readers_only_observe_complete_snapshots():
    path = create_session_settings({"env": {"A": "0", "B": "0"}})

    def write():
        for index in range(100):
            value = str(index)
            update_session_settings(path, {"env": {"A": value, "B": value}})

    with ThreadPoolExecutor(max_workers=1) as executor:
        writer = executor.submit(write)
        for _ in range(200):
            snapshot = json.loads(path.read_text())
            assert snapshot["env"]["A"] == snapshot["env"]["B"]
        writer.result(timeout=10)
