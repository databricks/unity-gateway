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
import stat
import sys
import time
from pathlib import Path

LOG_PATH = Path.home() / ".ucode" / "auth-token-errors.log"
MAX_BYTES = 256 * 1024
# The caller waits for the helper to exit after signalling it, so the handler is capped: if the
# log write hangs, SIGALRM ends the process instead.
_HANDLER_DEADLINE_SECONDS = 5

_started_at = time.monotonic()
# ``time.perf_counter()`` when ``ucode.entrypoint`` started, for the time start-up took.
_entry_started: float | None = None
_stage = "starting"
_host: str | None = None
_profile: str | None = None


def set_entry_started(started: float) -> None:
    """Record the entry point's ``time.perf_counter()`` start, before the CLI is imported."""
    global _entry_started
    _entry_started = started


def startup_elapsed() -> float:
    """Seconds since the entry point started; zero when it did not record a start."""
    if _entry_started is None:
        return 0.0
    return max(0.0, time.perf_counter() - _entry_started)


def _clean(text: str) -> str:
    """One line of plain text: escape sequences and control characters dropped, spaces collapsed."""
    import re

    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b.?", "", text)
    return " ".join(re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", text).split())


def _without_userinfo(host: str) -> str:
    """``host`` with any ``user:password@`` removed."""
    from urllib.parse import urlsplit

    if "@" not in host:
        return host
    bare = "//" not in host
    try:
        parts = urlsplit("//" + host if bare else host)
        netloc = parts.netloc.rpartition("@")[2]
        cleaned = parts._replace(netloc=netloc).geturl()
    except ValueError:
        return host.rpartition("@")[2]
    return cleaned[2:] if bare and cleaned.startswith("//") else cleaned


def log_failure(host: str | None, profile: str | None, message: str) -> None:
    """Append one line to the log, best-effort; the file restarts once past ``MAX_BYTES``.

    The log is ``0600`` on POSIX and never written through a link."""
    path = LOG_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.lstat().st_size > MAX_BYTES:
                path.unlink()
        except FileNotFoundError:
            pass
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        host_label = _clean(_without_userinfo(host)) if host else ""
        profile_label = _clean(profile) if profile else ""
        line = (
            f"{stamp} host={host_label or '<unknown>'} profile={profile_label or '<none>'} "
            f"{_clean(message)}\n"
        )
        try:
            if stat.S_ISLNK(path.lstat().st_mode):
                return
        except FileNotFoundError:
            pass
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags | getattr(os, "O_BINARY", 0), 0o600)
        try:
            if os.name != "nt":
                os.fchmod(fd, 0o600)
            os.write(fd, line.encode("utf-8", "replace"))
        finally:
            os.close(fd)
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
            # Never wait for a lock another thread holds: the alarm would end the handler.
            subprocesses.end_running_trees(blocking=False)
        except Exception:
            pass
    log_failure(
        _host,
        _profile,
        f"killed by SIGTERM after {elapsed:.1f}s while {_stage}: the caller stopped waiting",
    )
    os.kill(os.getpid(), signum)
