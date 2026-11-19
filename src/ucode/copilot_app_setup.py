"""Sets up the GitHub Copilot desktop app on ``ug configure --agents copilot`` and undoes it.

The app's traffic goes to ug's login service, which runs anyway for Codex desktop: configure
records the app in the service's unit so Codex keeps it, and registers ug's providers at the
service's port, starting the app first when it's closed.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ucode import copilot_app, login_service
from ucode.agents.copilot import CopilotModel
from ucode.constants import LOOPBACK_HOST
from ucode.managed_files import OS, current_os
from ucode.os_compatibility import subprocess_cross_os
from ucode.ui import print_note, print_success, print_warning

_MACOS_BUNDLE_ID = "com.github.githubapp"
_LINUX_BINARY = Path("/usr/bin/github")
_WINDOWS_KEY = r"Software\GitHub Inc.\GitHub Copilot"
# How long configure waits for an app it started to open its WebSocket API.
LAUNCH_TIMEOUT_S = 30.0
LAUNCH_POLL_S = 0.5
_DETACHED: dict[str, Any] = (
    {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
    if sys.platform == "win32"
    else {"start_new_session": True}
)


def _windows_install_dir() -> str | None:
    """The app's registry ``InstallDir`` ("" when its key has none), or None without the key."""
    if sys.platform != "win32":
        return None
    import winreg

    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, _WINDOWS_KEY) as key:
                try:
                    value, _ = winreg.QueryValueEx(key, "InstallDir")
                except OSError:
                    value = ""
                return value if isinstance(value, str) else ""
        except OSError:
            continue
    return None


def installed() -> bool:
    """Whether the GitHub Copilot desktop app is installed for this user."""
    system = current_os()
    if system == OS.MACOS:
        return any(
            (root / "GitHub Copilot.app").is_dir()
            for root in (Path("/Applications"), Path.home() / "Applications")
        )
    if system == OS.WINDOWS:
        return _windows_install_dir() is not None
    if system == OS.LINUX:
        return _LINUX_BINARY.is_file()
    return False


def _launch_command() -> list[str] | None:
    """How to start the app on this machine, or None when ug can't start it."""
    system = current_os()
    if system == OS.MACOS:
        return ["open", "-g", "-b", _MACOS_BUNDLE_ID]
    if system == OS.WINDOWS:
        install_dir = _windows_install_dir()
        exe = Path(install_dir) / "github.exe" if install_dir else None
        return [str(exe)] if exe and exe.is_file() else None
    if system == OS.LINUX:
        # A window needs a display; over plain SSH there is none.
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return None
        return [str(_LINUX_BINARY)]
    return None


def _spawn(command: list[str]) -> None:
    subprocess_cross_os.popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **_DETACHED,
    )


def _register_after_launch(
    base_url: str, catalog: list[CopilotModel], provider_service: str | None
) -> None:
    """Start the closed app and register once its WebSocket API answers.

    Raises :class:`copilot_app.CopilotAppNotRunning` when it can't be started or doesn't come up
    in time."""
    command = _launch_command()
    if command is None:
        raise copilot_app.CopilotAppNotRunning("The GitHub Copilot app isn't running.")
    print_note("Starting the GitHub Copilot app to add the Databricks models to it...")
    try:
        _spawn(command)
    except OSError:
        raise copilot_app.CopilotAppNotRunning("The GitHub Copilot app didn't start.") from None
    deadline = time.monotonic() + LAUNCH_TIMEOUT_S
    while True:
        try:
            copilot_app.register(base_url, catalog, provider_service=provider_service)
            return
        except copilot_app.CopilotAppNotRunning:
            if time.monotonic() >= deadline:
                raise
            time.sleep(LAUNCH_POLL_S)


def configure(
    workspace: str, catalog: Iterable[CopilotModel], *, provider_service: str | None
) -> None:
    """Keep ug's login service up for the app on ``workspace`` and register ug's providers in it.

    Setup problems only warn, like the Codex app's."""
    catalog = list(catalog)
    port = login_service.recorded_port("port") or login_service.allocate_port()
    was_recorded = login_service.copilot_app_recorded()
    try:
        login_service.install(
            workspace,
            port,
            login_service.recorded_port("relay-port"),
            copilot_app=True,
        )
        if was_recorded:
            # The service reads the workspace's settings at start; an unchanged unit isn't reloaded.
            login_service.restart()
    except (OSError, RuntimeError, ValueError) as exc:
        print_warning(f"Could not set up ug's login service for the GitHub Copilot app: {exc}")
        return
    base_url = f"http://{LOOPBACK_HOST}:{port}"
    try:
        try:
            copilot_app.register(base_url, catalog, provider_service=provider_service)
        except copilot_app.CopilotAppNotRunning:
            _register_after_launch(base_url, catalog, provider_service)
    except copilot_app.CopilotAppNotRunning:
        print_note(
            "Open the GitHub Copilot app, then run `ug configure --agents copilot` again to add "
            "the Databricks models to it."
        )
        return
    except copilot_app.CopilotAppError as exc:
        print_warning(f"Could not add the Databricks models to the GitHub Copilot app: {exc}")
        return
    print_success(
        "GitHub Copilot app: added the Databricks models. Open its model picker to choose one."
    )


def remove(*, keep_service: bool) -> str:
    """Remove ug's providers from the app and its note from the login service unit.

    ``keep_service`` keeps the unit running for Codex desktop. Returns a summary for revert."""
    recorded = login_service.copilot_app_recorded()
    if not recorded and not (login_service.copilot_app_enabled() and installed()):
        return "not set up"
    try:
        result = "removed" if copilot_app.unregister() else "not set up"
    except copilot_app.CopilotAppNotRunning:
        result = "app closed; delete the Databricks providers in its settings"
        if not recorded:
            result = "not set up"
    except copilot_app.CopilotAppError as exc:
        result = f"failed: {exc}"
    if recorded:
        if keep_service:
            port = login_service.recorded_port("port")
            host, workspace_id = login_service.recorded_workspace()
            if port and host:
                # Keep the service exactly as recorded, not on the current workspace selection.
                login_service.install(
                    host,
                    port,
                    login_service.recorded_port("relay-port"),
                    pin=["--workspace-id", workspace_id] if workspace_id else [],
                )
        else:
            login_service.remove()
        if result == "not set up":
            result = "removed"
    return result


def status() -> str | None:
    """The app's row in ``ug status``, when ug set it up."""
    if not login_service.copilot_app_recorded():
        return None
    port = login_service.recorded_port("port")
    return f"set up (login service port {port})" if port else "set up"
