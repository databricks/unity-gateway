"""Failure log for the headless `ug auth-token` helper.

Agents run `auth-token` with no terminal and some (pi) discard its stderr, so
``~/.ucode/auth-token-errors.log`` is the only record of why a request went without a token. That
includes the caller giving up: pi sends SIGTERM once its 10s deadline passes, which by default ends
the helper before it can write anything.

This module must stay stdlib-only: ``ucode.entrypoint`` installs the SIGTERM handler before it
imports ``ucode.cli``.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

LOG_PATH = Path.home() / ".ucode" / "auth-token-errors.log"
MAX_BYTES = 256 * 1024
# The caller waits for the helper to exit after signalling it, so the handler is capped: if the
# log write hangs, SIGALRM ends the process instead.
_HANDLER_DEADLINE_SECONDS = 5

_started_at = time.monotonic()
_stage = "starting"
_host: str | None = None
_profile: str | None = None


def log_failure(host: str | None, profile: str | None, message: str) -> None:
    """Append one line to the log, best-effort; the file restarts once past ``MAX_BYTES``."""
    path = LOG_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > MAX_BYTES:
            path.unlink()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        line = " ".join(message.split())
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} host={host or '<unknown>'} profile={profile or '<none>'} {line}\n")
    except OSError:
        pass


def set_stage(stage: str, *, host: str | None = None, profile: str | None = None) -> None:
    """Record what the helper is doing now, for the kill handler's log line."""
    global _stage, _host, _profile
    _stage = stage
    if host is not None:
        _host = host
    if profile is not None:
        _profile = profile


def log_if_killed(args: list[str]) -> None:
    """Make a SIGTERM write a log line before the helper dies of it.

    ``args`` are the options after ``auth-token``; their ``--host`` and ``--profile`` label the
    line until :func:`set_stage` supplies resolved values. The process still dies by SIGTERM, so
    the caller sees the same exit status as before. No-op on Windows, where termination delivers
    no signal, and for the background refresher, which no caller waits on.
    """
    global _started_at, _host, _profile
    if sys.platform == "win32" or "--refresh-cache" in args:
        return
    _started_at = time.monotonic()
    _host = _option(args, "--host")
    _profile = _option(args, "--profile")
    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except (OSError, ValueError):
        pass  # not the main thread


def _option(args: list[str], name: str) -> str | None:
    for index, arg in enumerate(args):
        if arg == name and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith(name + "="):
            return arg[len(name) + 1 :]
    return None


def _on_sigterm(signum: int, _frame: object) -> None:
    # Default disposition first, so a second SIGTERM ends the process at once.
    signal.signal(signum, signal.SIG_DFL)
    if sys.platform != "win32":
        signal.alarm(_HANDLER_DEADLINE_SECONDS)
    elapsed = time.monotonic() - _started_at
    # A Databricks CLI call still running would outlive this process; end it first. Looked up
    # rather than imported: this module stays stdlib-only, and only a loaded CLI runs commands.
    subprocesses = sys.modules.get("ucode.os_compatibility.subprocess_cross_os")
    if subprocesses is not None:
        try:
            subprocesses.end_running_trees()
        except Exception:
            pass
    log_failure(
        _host,
        _profile,
        f"killed by SIGTERM after {elapsed:.1f}s while {_stage}: the caller stopped waiting",
    )
    os.kill(os.getpid(), signum)
