"""Tests for the cross-platform agent launcher (issue #173)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from ucode import launcher


class TestExecOrSpawn:
    def test_posix_explicit_environment_uses_execvpe(self):
        env = {"FEATURE_FLAG": "launch only"}
        with (
            patch.object(launcher.os, "name", "posix"),
            patch.object(launcher.os, "execvpe") as execvpe,
            patch.object(launcher.os, "execvp") as execvp,
        ):
            launcher.exec_or_spawn(["codex", "app-server"], env=env)
        execvpe.assert_called_once_with("codex", ["codex", "app-server"], env)
        execvp.assert_not_called()

    def test_windows_preserves_explicit_child_environment(self):
        env = {"FEATURE_FLAG": "launch only"}
        proc = MagicMock()
        proc.wait.return_value = 0
        with (
            patch.object(launcher.os, "name", "nt"),
            patch.object(launcher.subprocess, "Popen", return_value=proc) as popen,
            pytest.raises(SystemExit) as exc,
        ):
            launcher.exec_or_spawn(["codex.exe", "app-server"], env=env)
        assert exc.value.code == 0
        popen.assert_called_once_with(["codex.exe", "app-server"], env=env)

    def test_posix_uses_execvp(self):
        # On POSIX the agent process replaces ucode via execvp — no Popen.
        with (
            patch.object(launcher.os, "name", "posix"),
            patch.object(launcher.os, "execvp") as execvp,
            patch.object(launcher.subprocess, "Popen") as popen,
        ):
            launcher.exec_or_spawn(["claude", "--settings", "x"])
        execvp.assert_called_once_with("claude", ["claude", "--settings", "x"])
        popen.assert_not_called()

    def test_windows_spawns_and_waits(self):
        # On Windows there is no real exec; we must spawn + wait so the parent
        # shell does not resume and corrupt the terminal (issue #173).
        proc = MagicMock()
        proc.wait.return_value = 0
        with (
            patch.object(launcher.os, "name", "nt"),
            patch.object(launcher.os, "execvp") as execvp,
            patch.object(launcher.subprocess, "Popen", return_value=proc) as popen,
        ):
            with pytest.raises(SystemExit) as exc:
                launcher.exec_or_spawn(["claude.exe", "--settings", "x"])
        execvp.assert_not_called()
        popen.assert_called_once_with(["claude.exe", "--settings", "x"], env=None)
        proc.wait.assert_called_once()
        assert exc.value.code == 0

    def test_windows_propagates_child_exit_code(self):
        proc = MagicMock()
        proc.wait.return_value = 42
        with (
            patch.object(launcher.os, "name", "nt"),
            patch.object(launcher.subprocess, "Popen", return_value=proc),
        ):
            with pytest.raises(SystemExit) as exc:
                launcher.exec_or_spawn(["claude.exe"])
        assert exc.value.code == 42

    def test_windows_keyboard_interrupt_forwards_sigint(self):
        proc = MagicMock()
        # First wait() is interrupted; after forwarding SIGINT the child exits 130.
        proc.wait.side_effect = [KeyboardInterrupt(), 130]
        with (
            patch.object(launcher.os, "name", "nt"),
            patch.object(launcher.subprocess, "Popen", return_value=proc),
        ):
            with pytest.raises(SystemExit) as exc:
                launcher.exec_or_spawn(["claude.exe"])
        proc.send_signal.assert_called_once_with(launcher.signal.SIGINT)
        assert exc.value.code == 130


@pytest.mark.parametrize("platform", ["posix", "nt"])
def test_child_environment_inherits_without_mutation_and_handles_windows_case(platform):
    inherited = {"Feature_Flag": "native", "KEEP": "untouched"}
    with patch.dict(os.environ, inherited, clear=True), patch.object(launcher.os, "name", platform):
        child = launcher.build_child_env({"FEATURE_FLAG": "override", "EMPTY": ""})
        assert dict(os.environ) == inherited
    assert child["FEATURE_FLAG"] == "override"
    assert child["EMPTY"] == ""
    assert child["KEEP"] == "untouched"
    assert ("Feature_Flag" in child) == (platform == "posix")


def test_real_child_receives_exact_values_and_omission_restores_inheritance(monkeypatch, tmp_path):
    monkeypatch.setenv("LAUNCH_TEST_FLAG", "inherited")
    monkeypatch.setenv("LAUNCH_TEST_KEEP", "untouched")
    code = (
        "import json, os; "
        "print(json.dumps({key: os.environ.get(key) for key in "
        "['LAUNCH_TEST_FLAG', 'LAUNCH_TEST_EMPTY', 'LAUNCH_TEST_KEEP']}))"
    )
    command = [sys.executable, "-c", code]
    launch_code = (
        "import json, sys; from ucode.launcher import exec_or_spawn; "
        "exec_or_spawn(json.loads(sys.argv[1]), env=json.loads(sys.stdin.read()))"
    )
    for overlay, expected in [
        ({"LAUNCH_TEST_FLAG": "first\nline=2", "LAUNCH_TEST_EMPTY": ""}, "first\nline=2"),
        ({"LAUNCH_TEST_FLAG": "changed"}, "changed"),
        ({}, "inherited"),
    ]:
        child = launcher.build_child_env(overlay)
        result = subprocess.run(
            [sys.executable, "-c", launch_code, json.dumps(command)],
            input=json.dumps(child),
            text=True,
            capture_output=True,
            timeout=10,
            cwd=tmp_path,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {
            "LAUNCH_TEST_FLAG": expected,
            "LAUNCH_TEST_EMPTY": overlay.get("LAUNCH_TEST_EMPTY"),
            "LAUNCH_TEST_KEEP": "untouched",
        }
        parent_value = os.environ.get("LAUNCH_TEST_FLAG")
        assert parent_value == "inherited"
