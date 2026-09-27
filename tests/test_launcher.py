"""Tests for the cross-platform agent launcher (issue #173)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from ucode import launcher


class TestExecOrSpawn:
    @pytest.mark.parametrize("platform", ["posix", "nt"])
    def test_explicit_environment_uses_resolved_binary_and_copy(self, platform):
        environment = {"PATH": "/child/tools", "EMPTY": "", "LINES": "one\ntwo"}
        proc = MagicMock()
        proc.wait.return_value = 0
        with (
            patch.object(launcher.os, "name", platform),
            patch.object(
                launcher, "resolve_agent_argv", return_value=["/original/agent", "--help"]
            ) as resolve,
            patch.object(launcher.os, "execve") as execve,
            patch.object(launcher.subprocess, "Popen", return_value=proc) as popen,
        ):
            if platform == "nt":
                with pytest.raises(SystemExit):
                    launcher.exec_or_spawn(["agent", "--help"], env=environment)
                popen.assert_called_once_with(["/original/agent", "--help"], env=environment)
                assert popen.call_args.kwargs["env"] is not environment
            else:
                launcher.exec_or_spawn(["agent", "--help"], env=environment)
                execve.assert_called_once_with(
                    "/original/agent", ["/original/agent", "--help"], environment
                )
                assert execve.call_args.args[2] is not environment
            resolve.assert_called_once_with(["agent", "--help"])

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
        popen.assert_called_once_with(["claude.exe", "--settings", "x"])
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
