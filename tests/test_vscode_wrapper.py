"""Tests for ``ug-claude-vscode`` (ucode.vscode_wrapper), the VS Code extension's Claude launcher."""

from __future__ import annotations

import json
import shutil
import sys
import tomllib
from pathlib import Path

import pytest

from ucode import vscode_wrapper
from ucode.agents import claude


@pytest.fixture
def settings(monkeypatch, tmp_path):
    path = tmp_path / "ucode-settings.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(vscode_wrapper, "UG_CLAUDE_SETTINGS", path)
    return path


@pytest.fixture
def calls(monkeypatch):
    recorded: list[list[str]] = []

    def call(argv):
        recorded.append(argv)
        return 7

    monkeypatch.setattr(vscode_wrapper.subprocess, "call", call)
    return recorded


def _run(monkeypatch, *args: str) -> int:
    monkeypatch.setattr(vscode_wrapper.sys, "argv", ["ug-claude-vscode", *args])
    return vscode_wrapper.main()


def test_reads_the_settings_file_ug_writes():
    assert vscode_wrapper.UG_CLAUDE_SETTINGS == claude.CLAUDE_SETTINGS_PATH


def test_launches_the_binary_the_extension_passes(monkeypatch, tmp_path, settings, calls):
    binary = tmp_path / "claude.exe"
    binary.write_text("", encoding="utf-8")

    assert _run(monkeypatch, str(binary), "--output-format", "stream-json") == 7

    assert calls == [[str(binary), "--settings", str(settings), "--output-format", "stream-json"]]


def test_runs_a_javascript_build_with_node(monkeypatch, tmp_path, settings, calls):
    script = tmp_path / "cli.js"
    script.write_text("", encoding="utf-8")
    monkeypatch.setattr(vscode_wrapper.shutil, "which", lambda name: f"/bin/{name}")

    _run(monkeypatch, str(script), "-p", "hi")

    assert calls == [["/bin/node", str(script), "--settings", str(settings), "-p", "hi"]]


def test_falls_back_to_claude_on_path(monkeypatch, settings, calls):
    monkeypatch.setattr(vscode_wrapper.shutil, "which", lambda name: f"/bin/{name}")

    _run(monkeypatch, "-p", "hi")

    assert calls == [["/bin/claude", "--settings", str(settings), "-p", "hi"]]


def test_missing_settings_points_at_ug_configure(monkeypatch, tmp_path, calls):
    monkeypatch.setattr(vscode_wrapper, "UG_CLAUDE_SETTINGS", tmp_path / "missing.json")

    with pytest.raises(SystemExit, match="run `ug configure` first"):
        _run(monkeypatch, "-p", "hi")
    assert calls == []


def test_no_claude_anywhere(monkeypatch, settings, calls):
    monkeypatch.setattr(vscode_wrapper.shutil, "which", lambda name: None)

    with pytest.raises(SystemExit, match="`claude` is not on PATH"):
        _run(monkeypatch, "-p", "hi")


def test_stdout_stays_clean_and_a_second_settings_flag_is_flagged(
    monkeypatch, tmp_path, settings, calls, capsys
):
    binary = tmp_path / "claude"
    binary.write_text("", encoding="utf-8")

    _run(monkeypatch, str(binary), "--settings", "{}")

    captured = capsys.readouterr()
    assert captured.out == ""  # stdout is the extension's protocol channel
    assert "also passed --settings" in captured.err


def test_log_records_each_launch(monkeypatch, tmp_path, settings, calls):
    log = tmp_path / "launch.log"
    monkeypatch.setenv(vscode_wrapper.LOG_ENV_VAR, str(log))
    binary = tmp_path / "claude"
    binary.write_text("", encoding="utf-8")

    _run(monkeypatch, str(binary), "-p", "hi")

    assert json.loads(log.read_text(encoding="utf-8")) == [
        "ug-claude-vscode",
        str(binary),
        "-p",
        "hi",
    ]


def test_is_declared_as_a_console_script():
    pyproject = tomllib.loads(
        (Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert pyproject["project"]["scripts"]["ug-claude-vscode"] == "ucode.vscode_wrapper:main"


def test_console_script_is_installed_next_to_ug():
    bin_dir = Path(sys.executable).parent
    assert shutil.which("ug-claude-vscode", path=str(bin_dir)) is not None
