"""Restarts the GitHub Copilot app once no chat in it is running a turn.

The app's window loads its model picker's providers only when it starts, so ug's plugin starts this
(``ug copilot-app-restart``) after it changed ug's rows. It runs detached, because the app stops the
extension that starts it soon after the rows change.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from ucode.agents.desktop import copilot_app
from ucode.os_compatibility import subprocess_cross_os
from ucode.os_compatibility.system_cross_os import process_running

QUIET_S = 15.0
IDLE_TIMEOUT_S = 180.0
EXIT_TIMEOUT_S = 15.0
_POLL_S = 1.0
_EXIT_POLL_S = 0.1
_RELAUNCH_RETRY_S = 2.0
_LAUNCH_CHECK_S = 3.0
_COMMAND_TIMEOUT_S = 10.0
_PLATFORMS = ("linux", "darwin", "win32")
_TURN_STARTS = frozenset({"user.message", "assistant.turn_start"})
# The extension that starts this has the Copilot runtime's environment, with the runtime's own
# settings and credentials; the relaunched app gets only the user's session environment.
_SESSION_ENV = frozenset(
    {
        "HOME", "USER", "LOGNAME", "SHELL", "PATH", "LANG", "LANGUAGE", "TZ", "TMPDIR", "DISPLAY",
        "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "DESKTOP_SESSION",
        "SSH_AUTH_SOCK", "COPILOT_HOME", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
        "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERPROFILE",
        "USERNAME", "USERDOMAIN", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
        "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES",
        "COMMONPROGRAMFILES(X86)", "COMMONPROGRAMW6432", "ALLUSERSPROFILE", "PUBLIC",
        "COMPUTERNAME", "OS", "NUMBER_OF_PROCESSORS",
    }
)  # fmt: skip
_SESSION_ENV_PREFIXES = ("LC_", "XDG_", "PROCESSOR_")


@dataclass(frozen=True)
class Target:
    """The app to restart, as the plugin's extension found it."""

    platform: str
    pid: int
    # The executable to start again, or the bundle id on macOS.
    app: str
    home: Path
    log: Path | None = None


def parse_target(text: str) -> Target | None:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    platform, pid, app, home, log = (
        data.get(key) for key in ("platform", "pid", "app", "home", "log")
    )
    if (
        platform not in _PLATFORMS
        or not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 1
        or not isinstance(app, str)
        or not app
        or not isinstance(home, str)
    ):
        return None
    return Target(platform, pid, app, Path(home), Path(log) if isinstance(log, str) else None)


class Events(Protocol):
    def next_message(self, timeout: float) -> dict[str, Any] | None: ...

    def close(self) -> None: ...


def wait_for_idle(
    events: Events,
    *,
    quiet_s: float = QUIET_S,
    timeout_s: float = IDLE_TIMEOUT_S,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """``idle`` once no chat has a turn open and no session event has arrived for ``quiet_s``. A
    running turn sends events all the time, so that also covers a turn that started before the
    watch did. ``busy`` after ``timeout_s``, ``stopped`` when the app closes."""
    running: set[object] = set()
    start = last = clock()
    while True:
        now = clock()
        if not running and now - last >= quiet_s:
            return "idle"
        if now - start >= timeout_s:
            return "busy"
        try:
            message = events.next_message(_POLL_S)
        except copilot_app.CopilotAppNotRunning:
            return "stopped"
        if not isinstance(message, dict) or message.get("type") != "session_event":
            continue
        last = clock()
        event = message.get("event")
        kind = event.get("type") if isinstance(event, dict) else None
        if kind == "session.idle":
            running.discard(message.get("session_id"))
        elif kind in _TURN_STARTS:
            running.add(message.get("session_id"))


def session_env(env: Mapping[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in env.items()
        if name.upper() in _SESSION_ENV or name.upper().startswith(_SESSION_ENV_PREFIXES)
    }


def _run(argv: list[str]) -> bool:
    try:
        result = subprocess_cross_os.run(
            argv, capture_output=True, timeout=_COMMAND_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _launch(argv: list[str], env: Mapping[str, str]) -> bool:
    if sys.platform == "win32":
        detach: dict[str, Any] = {
            "creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        }
    else:
        detach = {"start_new_session": True}
    try:
        process = subprocess_cross_os.popen(
            argv,
            env=dict(env),
            cwd=str(Path.home()),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **detach,
        )
    except OSError:
        return False
    # A launch that fails exits at once and nonzero.
    try:
        return process.wait(timeout=_LAUNCH_CHECK_S) == 0
    except subprocess.TimeoutExpired:
        return True


def _terminate(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def restart_app(
    target: Target,
    *,
    alive: Callable[[int], bool] = process_running,
    terminate: Callable[[int], None] = _terminate,
    run: Callable[[list[str]], bool] = _run,
    launch: Callable[[list[str], Mapping[str, str]], bool] = _launch,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    env: Mapping[str, str] = os.environ,
) -> str:
    """Close the app and start it again once it has exited. It's never forced closed, so an app
    that stays up is left running."""
    if target.platform == "darwin":
        if not run(["/usr/bin/osascript", "-e", f'quit app id "{target.app}"']):
            terminate(target.pid)
    elif target.platform == "win32":
        # Without /F, taskkill closes the app's window as its close button does, which can leave
        # the app running in the tray.
        run(["taskkill", "/PID", str(target.pid)])
    else:
        terminate(target.pid)
    deadline = clock() + EXIT_TIMEOUT_S
    while alive(target.pid):
        if clock() >= deadline:
            return "still_running"
        sleep(_EXIT_POLL_S)
    argv = ["/usr/bin/open", "-b", target.app] if target.platform == "darwin" else [target.app]
    clean = session_env(env)
    for attempt in range(2):
        if attempt:
            sleep(_RELAUNCH_RETRY_S)
        if launch(argv, clean):
            return "relaunched"
    return "relaunch_failed"


def _watch(home: Path) -> Events:
    return copilot_app._AppConnection(home, session_channels=True)


def restart_when_idle(
    target: Target,
    *,
    watch: Callable[[Path], Events] = _watch,
    quiet_s: float = QUIET_S,
    timeout_s: float = IDLE_TIMEOUT_S,
    clock: Callable[[], float] = time.monotonic,
    **system: Any,
) -> str:
    """Restart the app once it's idle. A turn is visible only in the app's session events, so
    without them, or once the app closes, it gives up and leaves the app as it is."""
    try:
        events = watch(target.home)
    except copilot_app.CopilotAppNotRunning:
        return "not_running"
    except copilot_app.CopilotAppError:
        return "no_session_events"
    try:
        waited = wait_for_idle(events, quiet_s=quiet_s, timeout_s=timeout_s, clock=clock)
    finally:
        events.close()
    if waited != "idle":
        return waited
    return restart_app(target, clock=clock, **system)


def _log(path: Path | None, line: str) -> None:
    if path is None:
        return
    try:
        with path.open("a", encoding="utf-8") as file:
            file.write(f"{datetime.now(UTC).isoformat()} {line}\n")
    except OSError:
        pass


def main(argument: str) -> None:
    """``ug copilot-app-restart``: never raises; the outcome goes to the target's log."""
    target = parse_target(argument)
    if target is None:
        return
    try:
        outcome = restart_when_idle(target)
    except Exception as exc:  # noqa: BLE001 -- a detached helper can report only to its log
        outcome = f"failed: {exc}"
    _log(target.log, outcome)
