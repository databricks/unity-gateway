"""Component guards against rewriting identical agent config during launch."""

import os
from pathlib import Path
from unittest.mock import Mock

import pytest
import tomlkit

from ucode import config_io


@pytest.fixture(params=["text", "json", "toml", "dotenv"])
def write_config(request):
    def write(path, value):
        match request.param:
            case "text":
                config_io.write_text_file(path, value)
            case "json":
                config_io.write_json_file(path, {"key": value})
            case "toml":
                config_io.write_toml_file(path, tomlkit.document().add("key", value))
            case "dotenv":
                config_io.write_dotenv(path, {"KEY": value})

    return write


def test_identical_config_keeps_mtime_and_does_not_open_for_writing(
    tmp_path, monkeypatch, write_config
):
    """Scenario: repeated configured launch. Expected: no write or directory creation."""
    path = tmp_path / "config"
    write_config(path, "unchanged")
    os.utime(path, (1_000_000_000, 1_000_000_000))
    before = path.stat()
    write = Mock(side_effect=AssertionError("unchanged file must not be rewritten"))
    monkeypatch.setattr(Path, "write_text", write)
    monkeypatch.setattr(config_io, "ensure_parent_dir", write)

    write_config(path, "unchanged")

    assert path.stat().st_mtime_ns == before.st_mtime_ns
    assert path.stat().st_mode == before.st_mode
    write.assert_not_called()


def test_changed_missing_and_invalid_files_are_written(tmp_path, write_config):
    """Scenario: first launch, changed config, or invalid encoding. Expected: updated contents."""
    path = tmp_path / "nested" / "config"
    write_config(path, "old")
    before = path.read_bytes()
    write_config(path, "new")
    expected = path.read_bytes()
    assert expected != before
    path.write_bytes(b"\xff")
    write_config(path, "new")
    assert path.read_bytes() == expected


def test_unreadable_comparison_still_attempts_write(tmp_path, monkeypatch, write_config):
    """Scenario: a writable config cannot be read. Expected: existing write behavior survives."""
    path = tmp_path / "config"
    write_config(path, "old")
    monkeypatch.setattr(Path, "read_text", Mock(side_effect=PermissionError("read denied")))
    write_config(path, "new")
    assert b"new" in path.read_bytes()


def test_dry_run_shows_output_without_reading_or_writing(tmp_path, monkeypatch, write_config):
    """Scenario: dry-run preview. Expected: output remains visible without touching files."""
    monkeypatch.setattr(config_io, "_dry_run", True)
    output = Mock()
    monkeypatch.setattr(config_io.console, "print", output)
    read = Mock(side_effect=AssertionError("dry run must not read the target"))
    monkeypatch.setattr(Path, "read_text", read)
    path = tmp_path / "config"
    write_config(path, "preview")
    assert not path.exists()
    assert "preview" in output.call_args.args[0]
    read.assert_not_called()


def test_write_failure_remains_actionable(tmp_path, monkeypatch, write_config):
    """Scenario: changed config is unwritable. Expected: actionable error, not a skipped write."""
    path = tmp_path / "config"
    monkeypatch.setattr(Path, "write_text", Mock(side_effect=PermissionError("write denied")))
    with pytest.raises(RuntimeError, match="Failed to write config file"):
        write_config(path, "new")


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "latin-1"])
def test_text_comparison_honors_encoding(tmp_path, monkeypatch, encoding):
    """Scenario: an explicit text encoding. Expected: unchanged content skips the write."""
    path = tmp_path / "config"
    config_io.write_text_file(path, "caf\u00e9", encoding=encoding)
    monkeypatch.setattr(Path, "write_text", Mock(side_effect=AssertionError("unexpected write")))
    config_io.write_text_file(path, "caf\u00e9", encoding=encoding)


def test_equivalent_newlines_keep_original_bytes(tmp_path, monkeypatch):
    """Scenario: existing CRLF text matches rendered lines. Expected: no newline-only rewrite."""
    path = tmp_path / "config"
    path.write_bytes(b"unchanged\r\n")
    monkeypatch.setattr(Path, "write_text", Mock(side_effect=AssertionError("unexpected write")))
    config_io.write_text_file(path, "unchanged\n")
    assert path.read_bytes() == b"unchanged\r\n"
