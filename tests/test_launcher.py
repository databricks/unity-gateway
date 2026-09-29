"""Tests for the cross-platform agent launcher (issue #173)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from ucode import launcher


class TestExecOrSpawn:
    def test_posix_uses_execvp(self):
        # On POSIX the agent process replaces ucode via execvp — no Popen.
        with (
            patch.object(launcher.os, "name", "posix"),
            patch.object(launcher.os, "execvp") as execvp,
            patch.object(launcher.subprocess_compat, "popen") as popen,
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
            patch.object(launcher.subprocess_compat, "popen", return_value=proc) as popen,
        ):
            with pytest.raises(SystemExit) as exc:
                launcher.exec_or_spawn(["claude.exe", "--settings", "x"])
        execvp.assert_not_called()
        popen.assert_called_once_with(["claude.exe", "--settings", "x"])
        proc.wait.assert_called_once()
        assert exc.value.code == 0

    def test_windows_propagates_child_exit_code(self):
        proc = MagicMock()
        proc.wait.return_value = 42
        with (
            patch.object(launcher.os, "name", "nt"),
            patch.object(launcher.subprocess_compat, "popen", return_value=proc),
        ):
            with pytest.raises(SystemExit) as exc:
                launcher.exec_or_spawn(["claude.exe"])
        assert exc.value.code == 42

    def test_windows_passes_prompt_metacharacters_without_a_shell(self):
        proc = MagicMock()
        proc.wait.return_value = 0
        prompt = 'keep "quotes" & pipes | and %PATH% literal'
        argv = [r"C:\Program Files\Claude\claude.exe", "--print", prompt]
        with (
            patch.object(launcher.os, "name", "nt"),
            patch.object(launcher.subprocess_compat, "popen", return_value=proc) as popen,
        ):
            with pytest.raises(SystemExit) as exc:
                launcher.exec_or_spawn(argv)

        popen.assert_called_once_with(argv)
        assert exc.value.code == 0

    def test_windows_keyboard_interrupt_forwards_sigint(self):
        proc = MagicMock()
        # First wait() is interrupted; after forwarding SIGINT the child exits 130.
        proc.wait.side_effect = [KeyboardInterrupt(), 130]
        with (
            patch.object(launcher.os, "name", "nt"),
            patch.object(launcher.subprocess_compat, "popen", return_value=proc),
        ):
            with pytest.raises(SystemExit) as exc:
                launcher.exec_or_spawn(["claude.exe"])
        proc.send_signal.assert_called_once_with(launcher.signal.SIGINT)
        assert exc.value.code == 130

    def test_windows_spawns_resolved_npm_shim(self):
        proc = MagicMock()
        proc.wait.return_value = 0
        shim = r"C:\Users\me\AppData\Roaming\npm\codex.CMD"
        with (
            patch.object(launcher.os, "name", "nt"),
            patch.object(launcher.subprocess_compat.shutil, "which", return_value=shim),
            patch.object(
                launcher.subprocess_compat.subprocess, "Popen", return_value=proc
            ) as popen,
        ):
            with pytest.raises(SystemExit) as exc:
                launcher.exec_or_spawn(["codex", "--model", "m"])

        assert exc.value.code == 0
        popen.assert_called_once_with([shim, "--model", "m"])
