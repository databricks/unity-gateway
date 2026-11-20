"""Sets up the GitHub Copilot desktop app on ``ug configure --agents copilot`` and undoes it.

ug installs its Copilot plugin. Whenever the app runs, the plugin adds ug's providers to the app's
model picker and serves their models straight from the AI Gateway, so nothing of ug runs in the
background.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from ucode import copilot_app
from ucode import copilot_app_plugin as plugin
from ucode.config_io import is_dry_run
from ucode.env import env_bool
from ucode.managed_files import OS, current_os
from ucode.ui import print_note, print_success, print_warning

COPILOT_APP_ENV = "UG_EXPERIMENTAL_COPILOT_APP"

_LINUX_BINARY = Path("/usr/bin/github")
_WINDOWS_KEY = r"Software\GitHub Inc.\GitHub Copilot"
# User-level telemetry keys the app's runtime rejects; it then ignores the whole settings file,
# ug's plugin included.
_REJECTED_TELEMETRY_KEYS = ("enabled", "endpoint", "filePath", "protocol", "resourceAttributes")
_DRY_RUN_NOTE = "Dry run: the GitHub Copilot app isn't changed."
DRY_RUN_RESULT = "unchanged (dry run)"


def enabled() -> bool:
    return env_bool(COPILOT_APP_ENV)


def _windows_app_registered() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, _WINDOWS_KEY):
                return True
        except OSError:
            continue
    return False


def installed() -> bool:
    """Whether the GitHub Copilot desktop app is installed for this user."""
    system = current_os()
    if system == OS.MACOS:
        return any(
            (root / "GitHub Copilot.app").is_dir()
            for root in (Path("/Applications"), Path.home() / "Applications")
        )
    if system == OS.WINDOWS:
        return _windows_app_registered()
    if system == OS.LINUX:
        return _LINUX_BINARY.is_file()
    return False


def resolved_state(state: dict) -> dict:
    """``state`` with the workspace's managed Copilot settings layered on, as ``ug copilot``
    sees them: admin headers, the provider service and the app switch aren't in state.json."""
    from ucode.managed_config import load_managed_state
    from ucode.managed_resolve import resolve_state
    from ucode.workspace import state_workspace_key

    if managed := load_managed_state(state_workspace_key(state)):
        return resolve_state(managed, state, "copilot")
    return state


def plugin_config(state: dict, desired: copilot_app.DesiredProviders) -> plugin.PluginConfig:
    """The plugin's config for ``desired``, with the headers ``ug copilot`` sends."""
    from ucode.agents.copilot import proxy_headers
    from ucode.databricks import databricks_cli_path, ug_binary
    from ucode.workspace import selector_headers, workspace_headers

    workspace = state["workspace"]
    headers = {**proxy_headers(state), **desired.headers}
    # Last, so an admin header can't select another workspace. The app outlives this process's
    # workspace selection, so the saved id counts too.
    headers.update(workspace_headers(workspace))
    workspace_id = state.get("workspace_id")
    headers.update(selector_headers(workspace_id if isinstance(workspace_id, str) else None))
    ug_path = ug_binary()
    cli = databricks_cli_path()
    path_dirs: list[str] = []
    for executable in (ug_path, cli):
        if os.path.isabs(executable) and (directory := os.path.dirname(executable)):
            if directory not in path_dirs:
                path_dirs.append(directory)
    custom_oauth = state.get("custom_oauth")
    return plugin.PluginConfig(
        workspace=workspace,
        ug_path=ug_path,
        providers=desired.plugin,
        headers=headers,
        profile=state.get("profile"),
        path_dirs=tuple(path_dirs),
        databricks_path=cli if os.path.isabs(cli) else None,
        use_pat=bool(state.get("use_pat")),
        custom_oauth=custom_oauth if isinstance(custom_oauth, dict) else None,
        app_providers=desired.plugin_app,
        owned_provider_ids=copilot_app.OWNED_PROVIDER_IDS,
    )


def ignored_settings_reason(path: Path | None = None) -> str | None:
    """Why the app's runtime would ignore Copilot's ``settings.json``, and ug's plugin with it."""
    path = path or plugin.copilot_settings_path()
    try:
        settings = plugin.read_settings(path)
    except plugin.SettingsError:
        return f"{path} isn't valid JSON"
    telemetry = settings.get("telemetry")
    if isinstance(telemetry, dict):
        keys = [key for key in _REJECTED_TELEMETRY_KEYS if key in telemetry]
        if keys:
            names = ", ".join(f"telemetry.{key}" for key in keys)
            return f"{path} sets {names}, which the app only accepts from managed settings"
    return None


def configure(state: dict, *, provider: str | None, location: str | None) -> None:
    """Install ug's plugin in the app with the models the Copilot CLI was configured with.

    Problems only warn: the app never fails configure."""
    if is_dry_run():
        print_note(_DRY_RUN_NOTE)
        return
    state = resolved_state(state)
    try:
        desired = copilot_app.state_providers(state, provider=provider, location=location)
    except RuntimeError as exc:
        print_warning(f"Skipped the GitHub Copilot app: {exc}")
        return
    try:
        plugin.install(plugin_config(state, desired))
    except plugin.SettingsError as exc:
        print_warning(
            f"Skipped the GitHub Copilot app: {exc}, and ug doesn't rewrite a settings file it "
            "can't read. Fix it, then run `ug configure --agents copilot` again."
        )
        return
    except OSError as exc:
        print_warning(f"Could not install ug's plugin in the GitHub Copilot app: {exc}")
        return
    from ucode.databricks import databricks_cli_path
    from ucode.state import record_databricks_cli

    if os.path.isabs(cli := databricks_cli_path()):
        try:
            record_databricks_cli(cli)
        except RuntimeError as exc:
            print_warning(str(exc))
    if reason := ignored_settings_reason():
        print_warning(
            f"The GitHub Copilot app ignores its whole settings file while {reason}, so it won't "
            "load ug's plugin. Remove those keys."
        )
    print_success("GitHub Copilot app: installed ug's plugin with the Databricks models.")
    print_note(
        "Open (or restart) the GitHub Copilot app to load the Databricks models. If their names "
        "or icons look generic, restart it once more or open Settings -> Model providers."
    )


def remove() -> str:
    """Remove ug's providers from the app when it's running, then ug's plugin. Never starts the
    app. Returns a summary for revert."""
    if is_dry_run():
        print_note(_DRY_RUN_NOTE)
        return DRY_RUN_RESULT
    had_plugin = plugin.plugin_root().exists()
    removed = False
    problems: list[str] = []
    if had_plugin or installed():
        try:
            removed = copilot_app.unregister()
        except copilot_app.CopilotAppNotRunning:
            if had_plugin:
                problems.append(
                    "the app is closed, so the Databricks models stay in its picker until you "
                    "open it and run `ug revert` again"
                )
        except copilot_app.CopilotAppError as exc:
            problems.append(str(exc))
    plugin_removed = False
    try:
        plugin_removed = plugin.remove()
    except plugin.SettingsError as exc:
        if had_plugin:
            problems.append(f"{exc}; remove ug's unity-gateway entries from it yourself")
    if problems:
        return ("removed ug's plugin, but " if plugin_removed else "") + "; ".join(problems)
    return "removed" if removed or plugin_removed else "not set up"


@dataclass(frozen=True)
class AppStatus:
    """The app's row in ``ug status``, and what to tell the user when its turns can't work."""

    row: str
    warnings: tuple[str, ...] = ()


_PLUGIN_LABELS = {
    "installed": "installed",
    "disabled": "disabled",
    "not_installed": "not installed",
    "invalid_settings": "invalid settings",
}
_PLUGIN_PROBLEMS = {
    "disabled": "is turned off",
    "not_installed": "is missing",
    "invalid_settings": "isn't loaded, since Copilot's settings.json isn't valid JSON",
}
_REGISTRATION_LABELS = {
    "registered": "registered",
    "outdated": "out of date",
    "not_registered": "not registered",
    "not_running": "unknown (app closed)",
}


def status(state: dict, *, provider: str | None, location: str | None) -> AppStatus | None:
    """The app's row in ``ug status`` for ``state`` (managed settings already layered on), or None
    when ug doesn't set the app up."""
    if not enabled():
        return None
    if state.get("copilot_app_integration") is False:
        return AppStatus("off (workspace-managed)")
    plugin_status = plugin.status()
    if plugin_status == "not_installed" and not installed():
        return None
    try:
        desired = copilot_app.state_providers(state, provider=provider, location=location)
        registration = copilot_app.status(desired.app)
    except copilot_app.CopilotAppError as exc:
        registration = f"unknown ({exc})"
    except RuntimeError:
        registration = "unknown (no models to compare)"
    plugin_label = _PLUGIN_LABELS[plugin_status]
    warnings: list[str] = []
    ignored = ignored_settings_reason() if plugin_status != "invalid_settings" else None
    if ignored:
        plugin_label += ", ignored"
        warnings.append(
            f"The GitHub Copilot app ignores its whole settings file while {ignored}, so it "
            "doesn't load ug's plugin. Remove those keys."
        )
    problem = _PLUGIN_PROBLEMS.get(plugin_status) or ("isn't loaded" if ignored else None)
    if registration in ("registered", "outdated") and problem:
        warnings.append(
            f"ug's plugin for the GitHub Copilot app {problem}, but the Databricks models are in "
            "the app's picker, and their turns fail without it. Run `ug configure --agents "
            "copilot` again."
        )
    elif registration == "outdated":
        warnings.append(
            "The Databricks models in the GitHub Copilot app don't match this configuration. Run "
            "`ug configure --agents copilot` again."
        )
    row = f"plugin {plugin_label}, models {_REGISTRATION_LABELS.get(registration, registration)}"
    return AppStatus(row, tuple(warnings))
