"""Subprocess helpers for cross-platform command resolution, quoting, and text encoding."""

from __future__ import annotations

import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import threading
from pathlib import Path
from typing import Any

# Launch lines of npm's cmd-shim (the last line containing `%*`). A JS entry point
# run by node:   ... & "%_prog%"  "%dp0%\node_modules\@openai\codex\bin\codex.js" %*
# or a bundled native binary:   "%dp0%\node_modules\opencode-ai\bin\opencode.exe"   %*
# Older shims spell the directory `%~dp0` and name `node` / `"%~dp0\node.exe"` directly.
_SHIM_DIR = r'"%~?dp0%?\\'
_NPM_SHIM_EXE = re.compile(_SHIM_DIR + r'(?P<target>[^"]+\.exe)"\s+%\*', re.IGNORECASE)
_NPM_SHIM_NODE_SCRIPT = re.compile(
    r'(?:^|[&\s])(?:"%_prog%"|node|'
    + _SHIM_DIR
    + r'node\.exe")\s+'
    + _SHIM_DIR
    + r'(?P<target>[^"]+\.[cm]?js)"\s+%\*$',
    re.IGNORECASE,
)


def _unwrap_npm_shim(shim: str) -> list[str] | None:
    """Return direct argv to bypass cmd.exe argument re-parsing for npm shims."""
    try:
        lines = Path(shim).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    launch = next((line.strip() for line in reversed(lines) if "%*" in line), "")
    shim_dir = Path(shim).parent

    def in_shim_dir(relative: str) -> Path:
        return shim_dir.joinpath(*relative.split("\\"))

    if match := _NPM_SHIM_EXE.fullmatch(launch):
        target = in_shim_dir(match["target"])
        return [str(target)] if target.is_file() else None
    if match := _NPM_SHIM_NODE_SCRIPT.search(launch):
        script = in_shim_dir(match["target"])
        bundled_node = shim_dir / "node.exe"
        node = str(bundled_node) if bundled_node.is_file() else shutil.which("node")
        return [node, str(script)] if node and script.is_file() else None
    return None


def resolve_command(argv: list[str]) -> list[str]:
    if os.name != "nt" or not argv:
        # POSIX resolves commands itself; retain the original argv unchanged.
        return argv
    resolved = shutil.which(argv[0])
    if not resolved:
        return argv
    if resolved.lower().endswith(".cmd") and (program := _unwrap_npm_shim(resolved)):
        return [*program, *argv[1:]]
    return [resolved, *argv[1:]]


def shell_command(argv: list[str]) -> str:
    """Quote ``argv`` as one command string for the platform's shell."""
    if platform.system() == "Windows":
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


def _resolved_args(
    args: list[str] | str, *, shell: bool = False, executable: str | None = None
) -> list[str] | str:
    """Leave caller-controlled shell and executable interpretation unchanged."""
    if isinstance(args, list) and not shell and executable is None:
        return resolve_command(args)
    return args


def _with_text_defaults(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Avoid locale-dependent CLI text decoding unless the caller opts in."""
    if not any(kwargs.get(name) for name in ("text", "universal_newlines", "encoding", "errors")):
        return kwargs
    kwargs = kwargs.copy()
    kwargs.setdefault("encoding", "utf-8")
    kwargs.setdefault("errors", "replace")
    return kwargs


def run(args: list[str] | str, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    """Like subprocess.run, with Windows command resolution and UTF-8 text defaults."""
    kwargs = _with_text_defaults(kwargs)
    return subprocess.run(
        _resolved_args(
            args,
            shell=kwargs.get("shell", False),
            executable=kwargs.get("executable"),
        ),
        **kwargs,
    )


def popen(args: list[str] | str, **kwargs: Any) -> subprocess.Popen[Any]:
    """Like subprocess.Popen, with Windows command resolution and UTF-8 text defaults."""
    kwargs = _with_text_defaults(kwargs)
    return subprocess.Popen(
        _resolved_args(
            args,
            shell=kwargs.get("shell", False),
            executable=kwargs.get("executable"),
        ),
        **kwargs,
    )


# Commands `run_bounded` is waiting on, so a signal handler can end them before the process dies.
_RUNNING_TREES: set[subprocess.Popen[Any]] = set()
# Reentrant: a signal handler on the thread that holds it may call `end_running_trees`.
_RUNNING_TREES_LOCK = threading.RLock()
# After ending a tree, how long to wait for its output pipes to close.
_DRAIN_SECONDS = 5.0


def run_bounded(
    args: list[str], *, timeout: float, check: bool = False, **kwargs: Any
) -> subprocess.CompletedProcess[Any]:
    """Like ``run(args, capture_output=True, timeout=timeout)``, but a timeout ends the whole tree.

    ``subprocess.run`` kills only the direct child on a timeout, then waits for its output pipes
    to close. A wrapper's own child (the interpreter behind a Windows ``.cmd`` shim, or a shell
    script that does not ``exec``) keeps them open, so ``run`` waits as long as that child runs.
    The command gets its own process group (Windows) or session (POSIX), with no stdin unless the
    caller passes one, and is not meant to be interactive."""
    kwargs = _with_text_defaults(kwargs)
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    if os.name == "nt":
        kwargs["creationflags"] = (
            kwargs.get("creationflags", 0) | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        _resolved_args(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs
    )
    with _RUNNING_TREES_LOCK:
        _RUNNING_TREES.add(proc)
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _end_tree(proc)
            try:
                stdout, stderr = proc.communicate(timeout=_DRAIN_SECONDS)
            except subprocess.TimeoutExpired:
                # Something that left the tree still holds the pipes; stop waiting for it.
                stdout = stderr = None
            raise subprocess.TimeoutExpired(
                proc.args, timeout, output=stdout, stderr=stderr
            ) from None
        except BaseException:
            _end_tree(proc)
            raise
    finally:
        with _RUNNING_TREES_LOCK:
            _RUNNING_TREES.discard(proc)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
    returncode = proc.wait()
    if check and returncode:
        raise subprocess.CalledProcessError(returncode, proc.args, stdout, stderr)
    return subprocess.CompletedProcess(proc.args, returncode, stdout, stderr)


def end_running_trees() -> None:
    """End every command ``run_bounded`` is waiting on, e.g. before this process exits by signal."""
    with _RUNNING_TREES_LOCK:
        running = list(_RUNNING_TREES)
    for proc in running:
        _end_tree(proc)


def _end_tree(proc: subprocess.Popen[Any]) -> None:
    if proc.poll() is not None and os.name == "nt":
        return
    if os.name == "nt":
        from ucode.os_compatibility.system_cross_os import windows_system32_dir

        try:
            subprocess.run(
                [str(windows_system32_dir() / "taskkill.exe"), "/F", "/T", "/PID", str(proc.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_DRAIN_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            # The session leader's pid is its process group id.
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.kill()
    except OSError:
        pass
