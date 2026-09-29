"""Tests for the cross-platform agent launcher (issue #173)."""

from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from ucode import launcher


def windows_os_view() -> SimpleNamespace:
    """An ``os`` stand-in reporting ``name == "nt"``, to patch into the module under test.

    Patching the real ``os.name`` would make this test's own pathlib/tempfile work behave like
    Windows on Linux/macOS.
    """
    return SimpleNamespace(**{**vars(os), "name": "nt"})


class TestProcessWrappers:
    def test_run_resolves_windows_argv_and_preserves_kwargs(self):
        argv = ["codex", "--model", "m"]
        resolved = [r"C:\\node\\codex.exe", "--model", "m"]
        completed = MagicMock()
        with (
            patch.object(launcher.os, "name", "nt"),
            patch.object(launcher, "resolve_command", return_value=resolved) as resolve,
            patch.object(launcher.subprocess, "run", return_value=completed) as run,
        ):
            result = launcher.run(argv, check=False, capture_output=True, text=True)

        assert result is completed
        resolve.assert_called_once_with(argv)
        run.assert_called_once_with(resolved, check=False, capture_output=True, text=True)

    def test_popen_resolves_windows_argv_and_preserves_kwargs(self):
        argv = ["claude", "--settings", "settings.json"]
        resolved = [r"C:\\tools\\claude.exe", "--settings", "settings.json"]
        process = MagicMock()
        with (
            patch.object(launcher.os, "name", "nt"),
            patch.object(launcher, "resolve_command", return_value=resolved) as resolve,
            patch.object(launcher.subprocess, "Popen", return_value=process) as popen,
        ):
            result = launcher.popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={"TEST": "1"},
            )

        assert result is process
        resolve.assert_called_once_with(argv)
        popen.assert_called_once_with(
            resolved,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"TEST": "1"},
        )

    @pytest.mark.parametrize(
        "args, kwargs",
        [
            ("codex --help", {}),
            (["codex", "--help"], {"shell": True}),
            (["codex", "--help"], {"executable": "/bin/sh"}),
        ],
    )
    def test_run_preserves_strings_and_shell_or_executable_overrides(self, args, kwargs):
        with (
            patch.object(launcher.os, "name", "nt"),
            patch.object(launcher, "resolve_command") as resolve,
            patch.object(launcher.subprocess, "run", return_value=MagicMock()) as run,
        ):
            launcher.run(args, **kwargs)

        resolve.assert_not_called()
        run.assert_called_once_with(args, **kwargs)

    def test_popen_preserves_executable_override(self):
        argv = ["codex", "--help"]
        with (
            patch.object(launcher.os, "name", "nt"),
            patch.object(launcher, "resolve_command") as resolve,
            patch.object(launcher.subprocess, "Popen", return_value=MagicMock()) as popen,
        ):
            launcher.popen(argv, executable="/bin/sh")

        resolve.assert_not_called()
        popen.assert_called_once_with(argv, executable="/bin/sh")

    def test_posix_passes_argv_to_run_unchanged(self):
        argv = ["codex", "--help"]
        with (
            patch.object(launcher.os, "name", "posix"),
            patch.object(launcher, "resolve_command", return_value=argv) as resolve,
            patch.object(launcher.subprocess, "run", return_value=MagicMock()) as run,
        ):
            launcher.run(argv)

        resolve.assert_called_once_with(argv)
        run.assert_called_once_with(argv)

    def test_missing_command_keeps_subprocess_error(self):
        with pytest.raises(FileNotFoundError):
            launcher.run(["ucode-launcher-command-that-does-not-exist-872"])

    def test_real_run_preserves_text_and_bytes_results(self):
        command = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'launcher-ok\\n')"]

        text_result = launcher.run(command, check=True, capture_output=True, text=True, timeout=5)
        bytes_result = launcher.run(command, check=True, capture_output=True, timeout=5)

        assert text_result.returncode == 0
        assert text_result.stdout == "launcher-ok\n"
        assert bytes_result.returncode == 0
        assert bytes_result.stdout == b"launcher-ok\n"

    def test_real_popen_preserves_context_manager_and_exit_status(self):
        with launcher.popen(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(b'popen-ok\\n')",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as process:
            stdout, stderr = process.communicate(timeout=5)

        assert process.returncode == 0
        assert stdout == b"popen-ok\n"
        assert stderr == b""


class TestExecOrSpawn:
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
            patch.object(launcher.shutil, "which", return_value=None),
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

    def test_windows_passes_prompt_metacharacters_without_a_shell(self):
        proc = MagicMock()
        proc.wait.return_value = 0
        prompt = 'keep "quotes" & pipes | and %PATH% literal'
        argv = [r"C:\Program Files\Claude\claude.exe", "--print", prompt]
        with (
            patch.object(launcher.os, "name", "nt"),
            patch.object(launcher.subprocess, "Popen", return_value=proc) as popen,
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
            patch.object(launcher.subprocess, "Popen", return_value=proc),
        ):
            with pytest.raises(SystemExit) as exc:
                launcher.exec_or_spawn(["claude.exe"])
        proc.send_signal.assert_called_once_with(launcher.signal.SIGINT)
        assert exc.value.code == 130

    def test_windows_spawns_resolved_npm_shim(self):
        # A bare `codex` must reach Popen as the full `.cmd` path, or CreateProcess
        # raises FileNotFoundError on Windows.
        proc = MagicMock()
        proc.wait.return_value = 0
        shim = r"C:\Users\me\AppData\Roaming\npm\codex.CMD"
        with (
            patch.object(launcher.os, "name", "nt"),
            patch.object(launcher.shutil, "which", return_value=shim),
            patch.object(launcher.subprocess, "Popen", return_value=proc) as popen,
        ):
            with pytest.raises(SystemExit):
                launcher.exec_or_spawn(["codex", "--model", "m"])
        popen.assert_called_once_with([shim, "--model", "m"])


class TestResolveCommand:
    def test_windows_resolves_bare_name_to_full_path(self):
        shim = r"C:\Program Files\nodejs\npm.CMD"
        with (
            patch.object(launcher.os, "name", "nt"),
            patch.object(launcher.shutil, "which", return_value=shim) as which,
        ):
            argv = launcher.resolve_command(["npm", "install", "-g", "pkg"])
        assert argv == [shim, "install", "-g", "pkg"]
        which.assert_called_once_with("npm")

    def test_windows_leaves_argv_when_not_found(self):
        # Unresolvable programs pass through so the spawn raises its usual error.
        with (
            patch.object(launcher.os, "name", "nt"),
            patch.object(launcher.shutil, "which", return_value=None),
        ):
            assert launcher.resolve_command(["missing", "--x"]) == ["missing", "--x"]

    def test_posix_is_unchanged(self):
        with (
            patch.object(launcher.os, "name", "posix"),
            patch.object(launcher.shutil, "which") as which,
        ):
            assert launcher.resolve_command(["npm", "view"]) == ["npm", "view"]
        which.assert_not_called()

    def test_empty_argv(self):
        with patch.object(launcher.os, "name", "nt"):
            assert launcher.resolve_command([]) == []

    def test_windows_runs_an_npm_shims_program_directly(self, tmp_path):
        # Through cmd.exe, Codex's `hooks.PreToolUse=[{matcher = "Agent|..."}]` config
        # arg is split at `|` as a pipe; the unwrapped program receives it verbatim.
        shim, script = _write_shim(tmp_path, "codex.cmd", NODE_SCRIPT_SHIM, "codex.js")
        hook_arg = 'hooks.PreToolUse=[{matcher = "Agent|.*spawn_agent$"}]'

        def which(name):
            return {"codex": str(shim), "node": r"C:\node\node.exe"}.get(name)

        with (
            patch.object(launcher, "os", windows_os_view()),
            patch.object(launcher.shutil, "which", which),
        ):
            argv = launcher.resolve_command(["codex", "app-server", "--config", hook_arg])
        assert argv == [r"C:\node\node.exe", str(script), "app-server", "--config", hook_arg]


# Verbatim launch templates from npm's cmd-shim (npm 11), with the package path swapped.
NODE_SCRIPT_SHIM = r"""@ECHO off
GOTO start
:find_dp0
SET dp0=%~dp0
EXIT /b
:start
SETLOCAL
CALL :find_dp0

IF EXIST "%dp0%\node.exe" (
  SET "_prog=%dp0%\node.exe"
) ELSE (
  SET "_prog=node"
  SET PATHEXT=%PATHEXT:;.JS;=;%
)

endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  "%dp0%\node_modules\pkg\bin\{target}" %*
"""
NATIVE_EXE_SHIM = r"""@ECHO off
GOTO start
:find_dp0
SET dp0=%~dp0
EXIT /b
:start
SETLOCAL
CALL :find_dp0
"%dp0%\node_modules\pkg\bin\{target}"   %*
"""
# Older cmd-shim output: `%~dp0` spelled inline and `node` named directly.
LEGACY_NODE_SCRIPT_SHIM = r"""@IF EXIST "%~dp0\node.exe" (
  "%~dp0\node.exe"  "%~dp0\node_modules\pkg\bin\{target}" %*
) ELSE (
  @SETLOCAL
  @SET PATHEXT=%PATHEXT:;.JS;=;%
  node  "%~dp0\node_modules\pkg\bin\{target}" %*
)
"""


def _write_shim(tmp_path, name, template, target, *, create_target=True):
    target_path = tmp_path / "node_modules" / "pkg" / "bin" / target
    if create_target:
        target_path.parent.mkdir(parents=True)
        target_path.write_text("", encoding="utf-8")
    shim = tmp_path / name
    shim.write_text(template.replace("{target}", target), encoding="utf-8")
    return shim, target_path


class TestUnwrapNpmShim:
    def test_node_script_shim_runs_node_on_the_script(self, tmp_path):
        shim, script = _write_shim(tmp_path, "codex.cmd", NODE_SCRIPT_SHIM, "codex.js")
        with patch.object(launcher.shutil, "which", return_value="/opt/node") as which:
            assert launcher._unwrap_npm_shim(str(shim)) == ["/opt/node", str(script)]
        which.assert_called_once_with("node")

    def test_prefers_the_node_next_to_the_shim(self, tmp_path):
        # Mirrors the shim's own `IF EXIST "%dp0%\node.exe"` branch.
        shim, script = _write_shim(tmp_path, "codex.cmd", NODE_SCRIPT_SHIM, "codex.js")
        (tmp_path / "node.exe").write_text("", encoding="utf-8")
        with patch.object(launcher.shutil, "which") as which:
            argv = launcher._unwrap_npm_shim(str(shim))
        assert argv == [str(tmp_path / "node.exe"), str(script)]
        which.assert_not_called()

    def test_native_exe_shim_runs_the_exe(self, tmp_path):
        shim, exe = _write_shim(tmp_path, "opencode.cmd", NATIVE_EXE_SHIM, "opencode.exe")
        assert launcher._unwrap_npm_shim(str(shim)) == [str(exe)]

    def test_legacy_shim_layout(self, tmp_path):
        shim, script = _write_shim(tmp_path, "gemini.cmd", LEGACY_NODE_SCRIPT_SHIM, "cli.js")
        with patch.object(launcher.shutil, "which", return_value="/opt/node"):
            assert launcher._unwrap_npm_shim(str(shim)) == ["/opt/node", str(script)]

    @pytest.mark.parametrize(
        "launch_line",
        [
            # nodejs' own npm.cmd resolves its paths through variables.
            '"%NODE_EXE%" "%NPM_CLI_JS%" %*',
            # Flags between node and the script would be lost by unwrapping.
            '"%_prog%" --max-old-space-size=4096 "%dp0%\\node_modules\\pkg\\bin\\cli.js" %*',
        ],
    )
    def test_unrecognised_shims_run_through_cmd_as_before(self, tmp_path, launch_line):
        shim = tmp_path / "tool.cmd"
        shim.write_text(f"@ECHO off\n{launch_line}\n", encoding="utf-8")
        with patch.object(launcher.shutil, "which", return_value="/opt/node"):
            assert launcher._unwrap_npm_shim(str(shim)) is None

    def test_missing_target_runs_through_cmd_as_before(self, tmp_path):
        shim, _ = _write_shim(
            tmp_path, "codex.cmd", NODE_SCRIPT_SHIM, "codex.js", create_target=False
        )
        with patch.object(launcher.shutil, "which", return_value="/opt/node"):
            assert launcher._unwrap_npm_shim(str(shim)) is None

    def test_no_node_runs_through_cmd_as_before(self, tmp_path):
        shim, _ = _write_shim(tmp_path, "codex.cmd", NODE_SCRIPT_SHIM, "codex.js")
        with patch.object(launcher.shutil, "which", return_value=None):
            assert launcher._unwrap_npm_shim(str(shim)) is None

    def test_unreadable_shim(self, tmp_path):
        assert launcher._unwrap_npm_shim(str(tmp_path / "missing.cmd")) is None
