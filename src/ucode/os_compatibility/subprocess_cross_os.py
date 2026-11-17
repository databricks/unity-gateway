"""Subprocess helpers for cross-platform command resolution, quoting, and text encoding."""

from __future__ import annotations

import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
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
# After ending a tree, how long to wait for its output pipes to close. Short, so a pipe held by
# a process that escaped the tree adds little to the caller's timeout.
_DRAIN_SECONDS = 0.5
# How long the Windows fallback waits for `taskkill /T`.
_TASKKILL_SECONDS = 2.0
# Windows Job Objects holding each `run_bounded` tree, so ending one reaches every descendant
# even after the direct child exited.
_TREE_JOBS: dict[subprocess.Popen[Any], int] = {}
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


def _kernel32() -> Any:
    """kernel32 with the Job Object signatures declared, so 64-bit handles are not truncated."""
    if sys.platform != "win32":
        raise OSError("Job Objects are Windows-only")
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _assign_job(proc: subprocess.Popen[Any]) -> int | None:
    """Put ``proc`` in a new kill-on-close Job Object; return its handle, or None on failure.

    Assignment fails, for example, inside an older nested job that forbids breakaway; the tree
    is then ended with ``taskkill /T`` instead."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_ulonglong)
                for name in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class _BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class _ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimits),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = _kernel32()
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if kernel32.SetInformationJobObject(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ) and kernel32.AssignProcessToJobObject(job, int(proc._handle)):
            return int(job)
        kernel32.CloseHandle(job)
    except Exception:
        pass
    return None


def _close_job(job: int, *, terminate: bool) -> None:
    """Close a tree's Job Object, ending every process still in it first if ``terminate``."""
    try:
        kernel32 = _kernel32()
        if terminate:
            kernel32.TerminateJobObject(job, 1)
        kernel32.CloseHandle(job)
    except Exception:
        pass


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
    if os.name == "nt" and (job := _assign_job(proc)) is not None:
        _TREE_JOBS[proc] = job
    with _RUNNING_TREES_LOCK:
        _RUNNING_TREES.add(proc)
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _end_tree(proc)
            # Forgotten before the drain reaps it, so a signal handler never ends it again.
            _forget_tree(proc)
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
            _forget_tree(proc)
            raise
    finally:
        _forget_tree(proc)
        if (job := _TREE_JOBS.pop(proc, None)) is not None:
            # Kill-on-close: whatever the command left running ends with it.
            _close_job(job, terminate=False)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
    returncode = proc.wait()
    if check and returncode:
        raise subprocess.CalledProcessError(returncode, proc.args, stdout, stderr)
    return subprocess.CompletedProcess(proc.args, returncode, stdout, stderr)


def _forget_tree(proc: subprocess.Popen[Any]) -> None:
    with _RUNNING_TREES_LOCK:
        _RUNNING_TREES.discard(proc)


def end_running_trees(*, blocking: bool = True) -> None:
    """End every command ``run_bounded`` is waiting on, e.g. before this process exits by signal.

    ``blocking=False`` (from a signal handler) never waits for another thread holding the
    registry's lock; it reads a best-effort snapshot instead."""
    if _RUNNING_TREES_LOCK.acquire(blocking=blocking):
        try:
            running = list(_RUNNING_TREES)
        finally:
            _RUNNING_TREES_LOCK.release()
    else:
        try:
            running = list(_RUNNING_TREES)
        except RuntimeError:  # changed size while copying
            running = []
    for proc in running:
        _end_tree(proc)


def _end_tree(proc: subprocess.Popen[Any]) -> None:
    if os.name == "nt":
        if (job := _TREE_JOBS.pop(proc, None)) is not None:
            # Reaches a descendant that still holds the pipes after the direct child exited.
            _close_job(job, terminate=True)
            return
        if proc.poll() is not None:
            return
        from ucode.os_compatibility.system_cross_os import windows_system32_dir

        try:
            subprocess.run(
                [str(windows_system32_dir() / "taskkill.exe"), "/F", "/T", "/PID", str(proc.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_TASKKILL_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    elif proc.returncode is None:
        # The session leader's pid is its process group id. Until this process reaps the leader
        # (``returncode`` is then set), its pid can't be reused, so the group is still its own.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.kill()
    except OSError:
        pass
