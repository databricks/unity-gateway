"""Sets up the GitHub Copilot desktop app on ``ug configure --agents copilot`` and undoes it.

The app's traffic goes to ug's login service, which runs anyway for Codex desktop: configure
records the app in the service's unit so Codex keeps it, and registers ug's providers at the
service's port while the app runs.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from pathlib import Path

from ucode import copilot_app, login_service
from ucode.agents.copilot import CopilotModel
from ucode.constants import LOOPBACK_HOST
from ucode.managed_files import OS, current_os
from ucode.ui import print_note, print_success, print_warning

_DOCS = "https://github.com/databricks/unity-gateway/blob/main/docs/copilot-desktop-app.md"


def installed() -> bool:
    """Whether the GitHub Copilot desktop app is installed for this user."""
    system = current_os()
    if system == OS.MACOS:
        return any(
            (root / "GitHub Copilot.app").is_dir()
            for root in (Path("/Applications"), Path.home() / "Applications")
        )
    if system == OS.WINDOWS:
        if sys.platform != "win32":
            return False
        import winreg

        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, r"Software\GitHub Inc.\GitHub Copilot"):
                    return True
            except OSError:
                continue
        return False
    if system == OS.LINUX:
        return Path("/usr/bin/github").is_file()
    return False


def configure(
    workspace: str, catalog: Iterable[CopilotModel], *, provider_service: str | None
) -> None:
    """Keep ug's login service up for the app on ``workspace`` and register ug's providers in it.

    Setup problems only warn, like the Codex app's."""
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
    try:
        copilot_app.register(
            f"http://{LOOPBACK_HOST}:{port}", catalog, provider_service=provider_service
        )
    except copilot_app.CopilotAppNotRunning:
        print_note(
            "Open the GitHub Copilot app, then run `ug configure --agents copilot` again to add "
            "the Databricks models to it."
        )
        return
    except copilot_app.CopilotAppError as exc:
        print_warning(
            f"Could not add the Databricks models to the GitHub Copilot app: {exc} "
            f"To add them by hand, see {_DOCS}."
        )
        return
    print_success(
        "GitHub Copilot app: added the Databricks models. Open its model picker to choose one."
    )


def remove(*, keep_service: bool) -> str:
    """Remove ug's providers from the app and its note from the login service unit.

    ``keep_service`` keeps the unit running for Codex desktop. Returns a summary for revert."""
    recorded = login_service.copilot_app_recorded()
    if not recorded and not installed():
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
            if port and (workspace := login_service.recorded_workspace()[0]):
                login_service.install(workspace, port, login_service.recorded_port("relay-port"))
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
