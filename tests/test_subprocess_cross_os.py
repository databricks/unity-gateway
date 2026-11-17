"""Tests for cross-platform subprocess command resolution."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
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


class TestShellCommand:
    def test_windows_uses_cmd_quoting(self, monkeypatch):
        monkeypatch.setattr(subprocess_cross_os.platform, "system", lambda: "Windows")
        assert subprocess_cross_os.shell_command([r"C:\Program Files\ug.EXE", "route"]) == (
            r'"C:\Program Files\ug.EXE" route'
        )

    def test_posix_uses_shell_quoting(self, monkeypatch):
        monkeypatch.setattr(subprocess_cross_os.platform, "system", lambda: "Linux")
        assert subprocess_cross_os.shell_command(["/opt/my ug/ug", "route"]) == (
            "'/opt/my ug/ug' route"
        )


def _alive(pid: int) -> bool:
    if os.name == "nt":
        from ucode.os_compatibility.system_cross_os import windows_system32_dir

        listing = subprocess.run(
            [
                str(windows_system32_dir() / "tasklist.exe"),
                "/FI",
                f"PID eq {pid}",
                "/FO",
                "CSV",
                "/NH",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return f'"{pid}"' in listing.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wrapper_argv(pid_file, *, wrapper_exits: bool = False) -> list[str]:
    """A command whose own child holds the output pipes, like the python behind a `.cmd` shim.

    With ``wrapper_exits`` the wrapper exits once its child has started, leaving the child
    holding the pipes alone."""
    child = (
        "import os, sys, time\nopen(sys.argv[1], 'w').write(str(os.getpid()))\ntime.sleep(120)\n"
    )
    wrapper = (
        "import os, subprocess, sys, time\n"
        f"child = subprocess.Popen([sys.executable, '-c', {child!r}, sys.argv[1]])\n"
        "while not os.path.exists(sys.argv[1]):\n"
        "    time.sleep(0.05)\n"
    )
    if not wrapper_exits:
        wrapper += "child.wait()\n"
    return [sys.executable, "-c", wrapper, str(pid_file)]


class TestRunBounded:
    """`run_bounded` ends the whole process tree, so a wrapper's child can't hold its output open."""

    def test_returns_output_and_status(self):
        result = subprocess_cross_os.run_bounded(
            [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"],
            timeout=60,
            text=True,
        )
        assert (result.returncode, result.stdout.strip(), result.stderr.strip()) == (
            0,
            "out",
            "err",
        )
        with pytest.raises(subprocess.CalledProcessError):
            subprocess_cross_os.run_bounded(
                [sys.executable, "-c", "raise SystemExit(3)"], timeout=60, check=True
            )

    def test_timeout_ends_a_wrapper_and_its_child(self, tmp_path):
        pid_file = tmp_path / "child.pid"
        started = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            subprocess_cross_os.run_bounded(_wrapper_argv(pid_file), timeout=10, text=True)
        # The child would hold the pipes for two minutes; plain subprocess.run waits for it.
        assert time.monotonic() - started < 60
        child = int(pid_file.read_text())
        deadline = time.monotonic() + 30
        while _alive(child) and time.monotonic() < deadline:
            time.sleep(0.2)
        assert not _alive(child)

    def test_timeout_ends_a_child_its_exited_wrapper_left_behind(self, tmp_path):
        # The direct child is gone but its child still holds the pipes: ending the tree must
        # still reach it (a Job Object on Windows), and the drain must not add seconds.
        pid_file = tmp_path / "child.pid"
        timeout = 5.0
        started = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            subprocess_cross_os.run_bounded(
                _wrapper_argv(pid_file, wrapper_exits=True), timeout=timeout, text=True
            )
        assert time.monotonic() - started < timeout + 2.0
        child = int(pid_file.read_text())
        deadline = time.monotonic() + 30
        while _alive(child) and time.monotonic() < deadline:
            time.sleep(0.2)
        assert not _alive(child)

    def test_end_running_trees_ends_a_command_in_flight(self, tmp_path):
        pid_file = tmp_path / "child.pid"
        outcome: list[object] = []

        def run() -> None:
            try:
                outcome.append(
                    subprocess_cross_os.run_bounded(_wrapper_argv(pid_file), timeout=120)
                )
            except BaseException as exc:  # surfaced below
                outcome.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        deadline = time.monotonic() + 60
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pid_file.exists(), "the wrapper never started its child"
        subprocess_cross_os.end_running_trees()
        thread.join(timeout=60)
        assert not thread.is_alive()
        (result,) = outcome
        assert isinstance(result, subprocess.CompletedProcess)
        assert result.returncode != 0

    @pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
    def test_reaped_command_is_never_signalled_again(self, monkeypatch):
        # Once reaped, the leader's pid (its group id) may belong to an unrelated process.
        proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        proc.wait()
        killed: list[int] = []
        monkeypatch.setattr(subprocess_cross_os.os, "killpg", lambda pgid, sig: killed.append(pgid))
        subprocess_cross_os._end_tree(proc)
        assert killed == []

    @pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
    def test_timed_out_command_is_forgotten_before_it_is_reaped(self, monkeypatch):
        # A signal handler arriving while the drain reaps the command must not end it again.
        registered: list[bool] = []
        real_end_tree = subprocess_cross_os._end_tree

        def end_tree(proc):
            real_end_tree(proc)
            real_communicate = proc.communicate

            def communicate(*args, **kwargs):
                registered.append(proc in subprocess_cross_os._RUNNING_TREES)
                return real_communicate(*args, **kwargs)

            proc.communicate = communicate

        monkeypatch.setattr(subprocess_cross_os, "_end_tree", end_tree)
        with pytest.raises(subprocess.TimeoutExpired):
            subprocess_cross_os.run_bounded(
                [sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.5
            )
        assert registered == [False]
        assert subprocess_cross_os._RUNNING_TREES == set()

    def test_windows_tree_end_uses_the_job_even_after_the_wrapper_exited(self, monkeypatch):
        closed: list[tuple[int, bool]] = []
        killed: list[object] = []

        class Exited:
            pid = 1

            def poll(self) -> int:
                return 0

            def kill(self) -> None:
                pass

        exited = Exited()
        monkeypatch.setattr(subprocess_cross_os.os, "name", "nt")
        monkeypatch.setattr(
            subprocess_cross_os,
            "_close_job",
            lambda job, *, terminate: closed.append((job, terminate)),
        )
        monkeypatch.setattr(subprocess_cross_os.subprocess, "run", killed.append)
        monkeypatch.setattr(subprocess_cross_os, "_TREE_JOBS", {exited: 42})
        subprocess_cross_os._end_tree(exited)
        assert closed == [(42, True)]
        assert subprocess_cross_os._TREE_JOBS == {}
        # Without a job (assignment failed), an exited wrapper has no tree left to taskkill.
        subprocess_cross_os._end_tree(exited)
        assert closed == [(42, True)]
        assert killed == []

    def test_non_blocking_end_does_not_wait_for_a_held_lock(self, monkeypatch):
        # From the SIGTERM handler: another thread holding the registry lock must not stall it.
        ended: list[object] = []
        sentinel = object()
        monkeypatch.setattr(subprocess_cross_os, "_end_tree", ended.append)
        monkeypatch.setattr(subprocess_cross_os, "_RUNNING_TREES", {sentinel})
        held, release = threading.Event(), threading.Event()

        def hold() -> None:
            with subprocess_cross_os._RUNNING_TREES_LOCK:
                held.set()
                release.wait(timeout=60)

        thread = threading.Thread(target=hold)
        thread.start()
        try:
            assert held.wait(timeout=60)
            started = time.monotonic()
            subprocess_cross_os.end_running_trees(blocking=False)
            assert time.monotonic() - started < 5
            assert ended == [sentinel]
        finally:
            release.set()
            thread.join(timeout=60)
