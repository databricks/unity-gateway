"""Tests for cross-platform subprocess command resolution."""

from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from ucode.os_compatibility import subprocess_cross_os


def windows_os_view() -> SimpleNamespace:
    """Simulate Windows without changing pathlib's host platform."""
    return SimpleNamespace(**{**vars(os), "name": "nt"})


class TestProcessWrappers:
    def test_run_resolves_windows_argv_and_preserves_kwargs(self):
        argv = ["codex", "--model", "m"]
        resolved = [r"C:\\node\\codex.exe", "--model", "m"]
        completed = MagicMock()
        with (
            patch.object(subprocess_cross_os.os, "name", "nt"),
            patch.object(subprocess_cross_os, "resolve_command", return_value=resolved) as resolve,
            patch.object(subprocess_cross_os.subprocess, "run", return_value=completed) as run,
        ):
            result = subprocess_cross_os.run(argv, check=False, capture_output=True, text=True)

        assert result is completed
        resolve.assert_called_once_with(argv)
        run.assert_called_once_with(
            resolved,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def test_popen_resolves_windows_argv_and_preserves_kwargs(self):
        argv = ["claude", "--settings", "settings.json"]
        resolved = [r"C:\\tools\\claude.exe", "--settings", "settings.json"]
        process = MagicMock()
        with (
            patch.object(subprocess_cross_os.os, "name", "nt"),
            patch.object(subprocess_cross_os, "resolve_command", return_value=resolved) as resolve,
            patch.object(subprocess_cross_os.subprocess, "Popen", return_value=process) as popen,
        ):
            result = subprocess_cross_os.popen(
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
            patch.object(subprocess_cross_os.os, "name", "nt"),
            patch.object(subprocess_cross_os, "resolve_command") as resolve,
            patch.object(subprocess_cross_os.subprocess, "run", return_value=MagicMock()) as run,
        ):
            subprocess_cross_os.run(args, **kwargs)

        resolve.assert_not_called()
        run.assert_called_once_with(args, **kwargs)

    def test_popen_preserves_executable_override(self):
        argv = ["codex", "--help"]
        with (
            patch.object(subprocess_cross_os.os, "name", "nt"),
            patch.object(subprocess_cross_os, "resolve_command") as resolve,
            patch.object(
                subprocess_cross_os.subprocess, "Popen", return_value=MagicMock()
            ) as popen,
        ):
            subprocess_cross_os.popen(argv, executable="/bin/sh")

        resolve.assert_not_called()
        popen.assert_called_once_with(argv, executable="/bin/sh")

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            (
                {"universal_newlines": True},
                {"universal_newlines": True, "encoding": "utf-8", "errors": "replace"},
            ),
            (
                {"encoding": "cp1252"},
                {"encoding": "cp1252", "errors": "replace"},
            ),
            (
                {"errors": "ignore"},
                {"encoding": "utf-8", "errors": "ignore"},
            ),
        ],
    )
    def test_run_uses_text_defaults_for_all_text_mode_switches(self, kwargs, expected):
        argv = ["codex", "--help"]
        with patch.object(subprocess_cross_os.subprocess, "run", return_value=MagicMock()) as run:
            subprocess_cross_os.run(argv, **kwargs)

        run.assert_called_once_with(argv, **expected)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"text": True, "encoding": "cp1252", "errors": "strict"},
            {"text": True, "encoding": None, "errors": None},
        ],
    )
    def test_run_preserves_explicit_text_overrides(self, kwargs):
        argv = ["codex", "--help"]
        with patch.object(subprocess_cross_os.subprocess, "run", return_value=MagicMock()) as run:
            subprocess_cross_os.run(argv, **kwargs)

        run.assert_called_once_with(argv, **kwargs)

    def test_run_leaves_binary_mode_unchanged(self):
        argv = ["codex", "--help"]
        with patch.object(subprocess_cross_os.subprocess, "run", return_value=MagicMock()) as run:
            subprocess_cross_os.run(argv, capture_output=True, text=False)

        run.assert_called_once_with(argv, capture_output=True, text=False)

    def test_popen_uses_text_defaults(self):
        argv = ["codex", "--help"]
        with patch.object(
            subprocess_cross_os.subprocess, "Popen", return_value=MagicMock()
        ) as popen:
            subprocess_cross_os.popen(argv, stdin=subprocess.PIPE, text=True)

        popen.assert_called_once_with(
            argv, stdin=subprocess.PIPE, text=True, encoding="utf-8", errors="replace"
        )

    def test_posix_passes_argv_to_run_unchanged(self):
        argv = ["codex", "--help"]
        with (
            patch.object(subprocess_cross_os.os, "name", "posix"),
            patch.object(subprocess_cross_os, "resolve_command", return_value=argv) as resolve,
            patch.object(subprocess_cross_os.subprocess, "run", return_value=MagicMock()) as run,
        ):
            subprocess_cross_os.run(argv)

        resolve.assert_called_once_with(argv)
        run.assert_called_once_with(argv)

    def test_popen_resolves_windows_npm_shim(self):
        process = MagicMock()
        shim = r"C:\Users\me\AppData\Roaming\npm\codex.CMD"
        with (
            patch.object(subprocess_cross_os.os, "name", "nt"),
            patch.object(subprocess_cross_os.shutil, "which", return_value=shim),
            patch.object(subprocess_cross_os.subprocess, "Popen", return_value=process) as popen,
        ):
            result = subprocess_cross_os.popen(["codex", "--model", "m"])

        assert result is process
        popen.assert_called_once_with([shim, "--model", "m"])

    def test_missing_command_keeps_subprocess_error(self):
        with pytest.raises(FileNotFoundError):
            subprocess_cross_os.run(["ucode-launcher-command-that-does-not-exist-872"])

    def test_real_run_preserves_text_and_bytes_results(self):
        command = [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write('“run”'.encode('utf-8') + b'\\xff')",
        ]

        text_result = subprocess_cross_os.run(
            command, check=True, capture_output=True, text=True, timeout=5
        )
        bytes_result = subprocess_cross_os.run(command, check=True, capture_output=True, timeout=5)

        assert text_result.returncode == 0
        assert text_result.stdout == "“run”�"
        assert bytes_result.returncode == 0
        assert bytes_result.stdout == "“run”".encode() + b"\xff"

    def test_real_popen_preserves_context_manager_and_exit_status(self):
        with subprocess_cross_os.popen(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write('“popen”'.encode('utf-8') + b'\\xff')",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as process:
            stdout, stderr = process.communicate(timeout=5)

        assert process.returncode == 0
        assert stdout == "“popen”".encode() + b"\xff"
        assert stderr == b""

    def test_real_popen_decodes_utf8_and_replaces_invalid_bytes(self):
        with subprocess_cross_os.popen(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write('“popen”'.encode('utf-8') + b'\\xff')",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as process:
            stdout, stderr = process.communicate(timeout=5)

        assert process.returncode == 0
        assert stdout == "“popen”�"
        assert stderr == ""


class TestResolveCommand:
    def test_windows_resolves_bare_name_to_full_path(self):
        shim = r"C:\Program Files\nodejs\npm.CMD"
        with (
            patch.object(subprocess_cross_os.os, "name", "nt"),
            patch.object(subprocess_cross_os.shutil, "which", return_value=shim) as which,
        ):
            argv = subprocess_cross_os.resolve_command(["npm", "install", "-g", "pkg"])
        assert argv == [shim, "install", "-g", "pkg"]
        which.assert_called_once_with("npm")

    def test_windows_leaves_argv_when_not_found(self):
        with (
            patch.object(subprocess_cross_os.os, "name", "nt"),
            patch.object(subprocess_cross_os.shutil, "which", return_value=None),
        ):
            assert subprocess_cross_os.resolve_command(["missing", "--x"]) == ["missing", "--x"]

    def test_posix_is_unchanged(self):
        with (
            patch.object(subprocess_cross_os.os, "name", "posix"),
            patch.object(subprocess_cross_os.shutil, "which") as which,
        ):
            assert subprocess_cross_os.resolve_command(["npm", "view"]) == ["npm", "view"]
        which.assert_not_called()

    def test_empty_argv(self):
        with patch.object(subprocess_cross_os.os, "name", "nt"):
            assert subprocess_cross_os.resolve_command([]) == []

    def test_windows_runs_an_npm_shims_program_directly(self, tmp_path):
        # Through cmd.exe, Codex's `hooks.PreToolUse=[{matcher = "Agent|..."}]` config
        # arg is split at `|` as a pipe; the unwrapped program receives it verbatim.
        shim, script = _write_shim(tmp_path, "codex.cmd", NODE_SCRIPT_SHIM, "codex.js")
        hook_arg = 'hooks.PreToolUse=[{matcher = "Agent|.*spawn_agent$"}]'

        def which(name):
            return {"codex": str(shim), "node": r"C:\node\node.exe"}.get(name)

        with (
            patch.object(subprocess_cross_os, "os", windows_os_view()),
            patch.object(subprocess_cross_os.shutil, "which", which),
        ):
            argv = subprocess_cross_os.resolve_command(
                ["codex", "app-server", "--config", hook_arg]
            )
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
        with patch.object(subprocess_cross_os.shutil, "which", return_value="/opt/node") as which:
            assert subprocess_cross_os._unwrap_npm_shim(str(shim)) == ["/opt/node", str(script)]
        which.assert_called_once_with("node")

    def test_prefers_the_node_next_to_the_shim(self, tmp_path):
        shim, script = _write_shim(tmp_path, "codex.cmd", NODE_SCRIPT_SHIM, "codex.js")
        (tmp_path / "node.exe").write_text("", encoding="utf-8")
        with patch.object(subprocess_cross_os.shutil, "which") as which:
            argv = subprocess_cross_os._unwrap_npm_shim(str(shim))
        assert argv == [str(tmp_path / "node.exe"), str(script)]
        which.assert_not_called()

    def test_native_exe_shim_runs_the_exe(self, tmp_path):
        shim, exe = _write_shim(tmp_path, "opencode.cmd", NATIVE_EXE_SHIM, "opencode.exe")
        assert subprocess_cross_os._unwrap_npm_shim(str(shim)) == [str(exe)]

    def test_legacy_shim_layout(self, tmp_path):
        shim, script = _write_shim(tmp_path, "gemini.cmd", LEGACY_NODE_SCRIPT_SHIM, "cli.js")
        with patch.object(subprocess_cross_os.shutil, "which", return_value="/opt/node"):
            assert subprocess_cross_os._unwrap_npm_shim(str(shim)) == ["/opt/node", str(script)]

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
        with patch.object(subprocess_cross_os.shutil, "which", return_value="/opt/node"):
            assert subprocess_cross_os._unwrap_npm_shim(str(shim)) is None

    def test_missing_target_runs_through_cmd_as_before(self, tmp_path):
        shim, _ = _write_shim(
            tmp_path, "codex.cmd", NODE_SCRIPT_SHIM, "codex.js", create_target=False
        )
        with patch.object(subprocess_cross_os.shutil, "which", return_value="/opt/node"):
            assert subprocess_cross_os._unwrap_npm_shim(str(shim)) is None

    def test_no_node_runs_through_cmd_as_before(self, tmp_path):
        shim, _ = _write_shim(tmp_path, "codex.cmd", NODE_SCRIPT_SHIM, "codex.js")
        with patch.object(subprocess_cross_os.shutil, "which", return_value=None):
            assert subprocess_cross_os._unwrap_npm_shim(str(shim)) is None

    def test_unreadable_shim(self, tmp_path):
        assert subprocess_cross_os._unwrap_npm_shim(str(tmp_path / "missing.cmd")) is None
