"""Best-effort Desktop configuration and cleanup for CLI commands.

For the GitHub Copilot app, ug installs its Copilot plugin. Whenever the app runs, the plugin adds
ug's providers to the app's model picker and serves their models straight from the AI Gateway, so
nothing of ug runs in the background.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from collections import Counter
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

from ucode.agents.copilot import CopilotModel, claude_model_name
from ucode.agents.desktop import copilot_app, copilot_app_plugin, copilot_app_tracing
from ucode.agents.desktop.claude import (
    can_initialize_claude_desktop_directory,
    claude_desktop_directory,
    revert_claude_desktop,
)
from ucode.agents.desktop.claude import (
    configure_claude_desktop as write_claude_desktop_profile,
)
from ucode.agents.desktop.cowork import configure_code, configure_cowork, revert_code, revert_cowork
from ucode.config_io import is_dry_run
from ucode.constants import (
    MODEL_PROVIDER_SERVICE_HEADER,
    MODEL_SERVICE_PARENT_SCHEMA_HEADER,
)
from ucode.databricks import build_tool_base_url, get_databricks_token, list_anthropic_model_catalog
from ucode.managed_files import OS, ManagedFileWriteUnavailable, current_os, machine_wide_only
from ucode.state import desktop_uses_claude_subscription_relay, get_provider_service
from ucode.string_utils import model_picker_label
from ucode.ui import print_kv, print_note, print_success, print_warning
from ucode.workspace import workspace_headers, workspace_id_args

_CLAUDE_MODEL_PATTERN = re.compile(r"\bclaude-", re.IGNORECASE)


def configure_claude_desktop(state: dict, *, parent_schema: str | None = None) -> None:
    if sys.platform not in ("darwin", "win32", "linux"):
        return
    try:
        if machine_wide_only():
            if sys.platform != "darwin":
                print_note(
                    "Skipped Claude Desktop: --global MDM provisioning currently supports macOS only."
                )
                return
            from ucode.agents.desktop import mdm

            if state.get("use_pat") or state.get("custom_oauth"):
                raise RuntimeError(
                    "Desktop MDM provisioning does not support PAT/custom OAuth profiles."
                )
            mdm.configure(state["workspace"], state.get("profile"))
            if not is_dry_run():
                print_success(
                    "Claude Desktop MDM deployment configured. Users authenticate when they open Desktop."
                )
            return
        if is_dry_run():
            print_note("Would configure Claude Desktop alongside Claude Code.")
            return
        _configure_claude_desktop_user_level(state, parent_schema=parent_schema)
    except Exception as exc:  # noqa: BLE001 -- Desktop must not block Claude Code setup
        print_warning(f"Could not configure Claude Desktop: {exc}. Claude Code setup is unchanged.")


def configure_claude_code_plugin(mcp_servers: list[dict]) -> None:
    if sys.platform not in ("darwin", "win32", "linux"):
        return
    if machine_wide_only():
        return
    if is_dry_run():
        print_note("Would configure Claude Code connector guidance.")
        return
    try:
        configured = configure_code(mcp_servers)
    except Exception as exc:  # noqa: BLE001 -- Plugin setup must not block MCP configuration
        print_warning(f"Could not configure Claude Code connector plugin: {exc}.")
        return
    if configured:
        print_success("Claude Code connector plugin configured. Start a new Code session.")


def configure_claude_cowork(
    state: dict, mcp_servers: list[dict], *, sync_plugin: bool = True
) -> None:
    if sys.platform not in ("darwin", "win32", "linux"):
        return
    if machine_wide_only():
        # The Cowork plugin is installed for the user's own Claude account.
        return
    if is_dry_run():
        print_note("Would configure Claude Cowork workspace skills and MCP servers.")
        return
    try:
        configured = configure_cowork(state, mcp_servers, sync_plugin=sync_plugin)
    except Exception as exc:  # noqa: BLE001 -- Cowork must not block Claude Code setup
        print_warning(
            f"Could not configure Claude Desktop workspace plugin: {exc}. Claude Code setup is unchanged."
        )
        return
    if configured:
        print_success("Claude Desktop workspace plugin configured. Quit and reopen Claude Desktop.")


def revert_claude_cowork() -> str:
    if sys.platform not in ("darwin", "win32", "linux") or is_dry_run():
        return "unchanged"
    try:
        return "removed" if revert_cowork() else "unchanged"
    except Exception as exc:  # noqa: BLE001 -- Preserve other cleanup operations on failure
        print_warning(f"Could not revert Claude Cowork: {exc}. Cowork setup was retained.")
        return "failed"


def revert_claude_code_plugin() -> str:
    if sys.platform not in ("darwin", "win32", "linux") or is_dry_run():
        return "unchanged"
    try:
        return "removed" if revert_code() else "unchanged"
    except Exception as exc:  # noqa: BLE001 -- Preserve other cleanup operations on failure
        print_warning(f"Could not revert Claude Code connector plugin: {exc}.")
        return "failed"


def _configure_claude_desktop_user_level(state: dict, *, parent_schema: str | None) -> None:
    directory = claude_desktop_directory()
    if directory is None:
        raise RuntimeError("Claude Desktop profile configuration is unavailable on this platform.")
    if sys.platform != "darwin" and not can_initialize_claude_desktop_directory(directory):
        raise RuntimeError("Open Claude Desktop once, then run ug configure again.")
    relayed = desktop_uses_claude_subscription_relay(state)
    claude_binary = None
    if relayed:
        claude_binary = shutil.which("claude")
        if not claude_binary or not os.path.isabs(claude_binary):
            raise RuntimeError("Install Claude Code before configuring subscription relay.")
    if state.get("custom_oauth"):
        raise RuntimeError("Desktop does not support custom OAuth profiles yet.")
    use_pat = bool(state.get("use_pat"))
    workspace = state.get("workspace")
    profile = state.get("profile")
    if not isinstance(workspace, str) or not workspace:
        raise RuntimeError("No Databricks workspace is configured.")
    if not isinstance(profile, str) or not profile:
        raise RuntimeError("Select an explicit Databricks CLI profile and run ug configure again.")

    settings = resolve_gateway_profile(state, parent_schema=parent_schema)

    write_claude_desktop_profile(
        **settings,
        helper_command=os.path.abspath(sys.executable),
        helper_args=[
            "-m",
            "ucode.cli",
            "claude-desktop-auth",
            "--host",
            workspace,
            "--profile",
            profile,
            *(["--relay", "--claude-binary", claude_binary] if claude_binary else []),
            *(["--use-pat"] if use_pat else []),
        ],
        otel_headers_command=[
            os.path.abspath(sys.executable),
            "-m",
            "ucode.cli",
            "otel-headers",
            "--host",
            workspace,
            *workspace_id_args(workspace),
            "--profile",
            profile,
            *(["--use-pat"] if use_pat else []),
        ],
        profile_key=profile,
    )
    print_success("Claude Desktop gateway profile configured. Quit and reopen Claude Desktop.")


def resolve_gateway_profile(
    state: dict, *, parent_schema: str | None = None, token: str | None = None
) -> dict:
    workspace = state["workspace"]
    profile = state.get("profile")
    provider = None if parent_schema else get_provider_service(state, "claude")
    static_models = (
        state.get("claude_static_models") if not provider and not parent_schema else None
    )
    token = token if token is not None else get_databricks_token(workspace, profile)
    catalog = list_anthropic_model_catalog(
        workspace, token, provider=provider, parent_schema=parent_schema
    )
    if static_models:
        model_ids = static_models
    else:
        if not catalog.model_ids:
            raise RuntimeError(catalog.error_msg or "Gateway model discovery returned no models.")
        model_ids = catalog.model_ids

    model_ids = [model_id for model_id in model_ids if _CLAUDE_MODEL_PATTERN.search(model_id)]
    if not model_ids:
        raise RuntimeError(
            "No Claude-family models found for Desktop. "
            "Add a Claude model to your model source and run ug configure again."
        )

    display_names = {}
    for model_id in model_ids:
        label = catalog.model_id_to_display_name.get(model_id)
        if not label or label == model_id:
            label = model_picker_label(claude_model_name(model_id))
        display_names[model_id] = label
    label_counts = Counter(display_names.values())
    display_names = {
        model_id: model_id if label_counts[label] > 1 else label
        for model_id, label in display_names.items()
    }

    headers = {"x-databricks-use-coding-agent-mode": "true"}
    if provider:
        headers[MODEL_PROVIDER_SERVICE_HEADER] = provider
    elif parent_schema:
        headers[MODEL_SERVICE_PARENT_SCHEMA_HEADER] = parent_schema
    for name, value in {
        **(state.get("claude_http_headers") or {}),
        **workspace_headers(workspace),
    }.items():
        existing = next((key for key in headers if key.casefold() == name.casefold()), None)
        if existing:
            del headers[existing]
        headers[name] = value

    return {
        "model_ids": model_ids,
        "model_id_to_display_name": display_names,
        "base_url": build_tool_base_url("claude", workspace.rstrip("/")),
        "custom_headers": headers,
        "otel_endpoint": (
            f"{workspace.rstrip('/')}/ai-gateway/otel" if state.get("claude_otel_tracing") else None
        ),
    }


def revert_claude_desktop_mdm() -> str:
    if sys.platform != "darwin":
        return "unchanged"
    from ucode.agents.desktop import mdm

    try:
        return "removed" if mdm.remove() else "unchanged"
    except ManagedFileWriteUnavailable as exc:
        print_warning(
            f"Claude Desktop MDM is installed machine-wide; an administrator must run "
            f"`ug revert` to remove it ({exc})."
        )
        return "skipped"
    except Exception as exc:  # noqa: BLE001 -- Preserve other revert operations on failure
        print_warning(
            f"Could not revert Claude Desktop MDM: {exc}. Fix it, then run ug revert again."
        )
        return "failed"


def revert_desktop() -> str:
    if sys.platform not in ("darwin", "win32", "linux") or is_dry_run():
        return "unchanged"
    results = [revert_claude_code_plugin(), revert_claude_cowork(), revert_claude_desktop_mdm()]
    if claude_desktop_directory() is None:
        if "failed" in results:
            return "failed"
        if "removed" in results:
            return "removed"
        return "skipped" if "skipped" in results else "unchanged"
    try:
        result = revert_claude_desktop()
    except Exception as exc:  # noqa: BLE001 -- Preserve other revert operations on failure
        print_warning(f"Could not revert Claude Desktop: {exc}. Desktop setup was retained.")
        return "failed"
    native_result = "removed" if result.changed else "unchanged"
    if "failed" in results:
        return "failed"
    if "removed" in (native_result, *results):
        return "removed"
    return "skipped" if "skipped" in results else "unchanged"


_COPILOT_APP_LINUX_BINARIES = (Path("/usr/bin/github"), Path("/opt/GitHub Copilot/github"))
# The app writes this desktop entry at its first run; a user's own launcher entry names the app.
_COPILOT_APP_LINUX_DESKTOP_FILE = "com.github.githubapp.desktop"
_COPILOT_APP_WINDOWS_KEY = r"Software\GitHub Inc.\GitHub Copilot"
# User-level telemetry keys the app's runtime rejects; it then ignores the whole settings file,
# ug's plugin included.
_COPILOT_REJECTED_TELEMETRY_KEYS = (
    "enabled",
    "endpoint",
    "filePath",
    "protocol",
    "resourceAttributes",
)
_COPILOT_APP_DRY_RUN_NOTE = "Dry run: the GitHub Copilot app isn't changed."
COPILOT_APP_DRY_RUN = "unchanged (dry run)"


def _copilot_app_windows_registered() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, _COPILOT_APP_WINDOWS_KEY):
                return True
        except OSError:
            continue
    return False


def _linux_desktop_entry_dirs() -> tuple[Path, ...]:
    return (Path("/usr/share/applications"), Path.home() / ".local" / "share" / "applications")


def _linux_appimage_dirs() -> tuple[Path, ...]:
    return (Path.home() / "Applications", Path.home() / ".local" / "bin")


def _copilot_app_desktop_entry(path: Path) -> bool:
    """Whether a `.desktop` entry launches the GitHub Copilot app: the app's own file, or an entry
    named after the app whose command is its `github` binary."""
    if path.name == _COPILOT_APP_LINUX_DESKTOP_FILE:
        return True
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    name = re.search(r"^Name=(.*)$", text, re.MULTILINE)
    command = re.search(r"^Exec=(.*)$", text, re.MULTILINE)
    if name is None or command is None or "github copilot" not in name.group(1).lower():
        return False
    return any(
        token.strip("\"'") == "github" or token.strip("\"'").endswith("/github")
        for token in command.group(1).split()
    )


def _copilot_app_linux_location() -> Path | None:
    for binary in _COPILOT_APP_LINUX_BINARIES:
        if binary.is_file():
            return binary
    for directory in _linux_desktop_entry_dirs():
        for entry in sorted(directory.glob("*.desktop")):
            if _copilot_app_desktop_entry(entry):
                return entry
    for directory in _linux_appimage_dirs():
        for image in sorted(directory.glob("*")):
            lowered = image.name.lower()
            if lowered.endswith(".appimage") and "github" in lowered and "copilot" in lowered:
                return image
    return None


def copilot_app_location() -> Path | None:
    """Where the GitHub Copilot desktop app was found for this user, or None: its install
    location for this OS, or its run directory while it is open somewhere else."""
    system = current_os()
    if system == OS.MACOS:
        for root in (Path("/Applications"), Path.home() / "Applications"):
            if (root / "GitHub Copilot.app").is_dir():
                return root / "GitHub Copilot.app"
    elif system == OS.WINDOWS:
        if _copilot_app_windows_registered():
            return Path(_COPILOT_APP_WINDOWS_KEY)
    elif system == OS.LINUX:
        if (found := _copilot_app_linux_location()) is not None:
            return found
    else:
        return None
    if copilot_app.app_running():
        return copilot_app.copilot_home() / "run"
    return None


def copilot_app_installed() -> bool:
    """Whether the GitHub Copilot desktop app is installed for this user."""
    return copilot_app_location() is not None


def copilot_app_state(state: dict) -> dict:
    """``state`` with the workspace's managed Copilot settings layered on, as ``ug copilot``
    sees them: admin headers, the provider service and the app switch aren't in state.json."""
    from ucode.managed_config import load_managed_state
    from ucode.managed_resolve import resolve_state
    from ucode.workspace import state_workspace_key

    if managed := load_managed_state(state_workspace_key(state)):
        return resolve_state(managed, state, "copilot")
    return state


def _with_expanded_sources(state: dict) -> dict:
    """``state`` with the managed list's wildcard sources (``<catalog>.<schema>.*``) replaced by
    what configure last expanded them to. Re-layering the managed config brings the raw sources
    back, and a list of only wildcards would read as no list, so the app would get every
    discovered model instead of the admin's. A source never expanded fails closed."""
    from ucode.agents.model_sources import is_model_source_wildcard
    from ucode.state import get_managed_model_source_expansions

    sources = state.get("copilot_static_models")
    if not isinstance(sources, list) or not any(
        isinstance(source, str) and is_model_source_wildcard(source) for source in sources
    ):
        return state
    expansions = get_managed_model_source_expansions(state, "copilot")
    models: list[str] = []
    for source in sources:
        if not isinstance(source, str) or not is_model_source_wildcard(source):
            models.append(source)
        elif source in expansions:
            models.extend(expansions[source])
        else:
            raise RuntimeError(
                f"the workspace's managed model source {source} isn't expanded yet. Run "
                "`ug configure --agents copilot` again"
            )
    return {**state, "copilot_static_models": list(dict.fromkeys(models))}


def copilot_app_plugin_config(
    state: dict, desired: copilot_app.DesiredProviders, *, seat_relay: str | None = None
) -> copilot_app_plugin.PluginConfig:
    """The plugin's config for ``desired``, with the headers ``ug copilot`` sends but its
    User-Agent. ``seat_relay`` is the relayed Model Provider Service the app's own GitHub Copilot
    calls go through."""
    from ucode.agents.copilot import proxy_headers
    from ucode.databricks import databricks_cli_path, running_launcher, ug_binary, windows_ug_exe
    from ucode.telemetry import ucode_user_agent, ug_version
    from ucode.workspace import selector_headers, workspace_headers

    workspace = state["workspace"]
    # Without one, the app's runtime sends its own User-Agent, as Claude Desktop does.
    headers = {
        name: value
        for name, value in {**proxy_headers(state), **desired.headers}.items()
        if name.casefold() != "user-agent"
    }
    # Last, so an admin header can't select another workspace. The app outlives this process's
    # workspace selection, so the saved id counts too.
    headers.update(workspace_headers(workspace))
    workspace_id = state.get("workspace_id")
    headers.update(selector_headers(workspace_id if isinstance(workspace_id, str) else None))
    ug_path = ug_binary()
    if current_os() is OS.WINDOWS:
        # The extension spawns ug without a shell, as the VS Code wrapper does.
        exe = windows_ug_exe(ug_path, running_launcher())
        if exe is None:
            raise RuntimeError(
                "ug's `ug.exe` wasn't found, and the GitHub Copilot app's plugin can only start "
                "an `.exe` without a shell. Reinstall ug, then rerun `ug configure --agents "
                "copilot`."
            )
        ug_path = exe
    cli = databricks_cli_path()
    path_dirs: list[str] = []
    for executable in (ug_path, cli):
        if os.path.isabs(executable) and (directory := os.path.dirname(executable)):
            if directory not in path_dirs:
                path_dirs.append(directory)
    custom_oauth = state.get("custom_oauth")
    return copilot_app_plugin.PluginConfig(
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
        seat_relay=seat_relay,
        user_agent=ucode_user_agent(ug_version()) if seat_relay else None,
    )


def copilot_settings_ignored_reason(path: Path | None = None) -> str | None:
    """Why the app's runtime would ignore Copilot's ``settings.json``, and ug's plugin with it."""
    path = path or copilot_app_plugin.copilot_settings_path()
    try:
        settings = copilot_app_plugin.read_settings(path)
    except copilot_app_plugin.SettingsError:
        return f"{path} isn't valid JSON"
    telemetry = settings.get("telemetry")
    if isinstance(telemetry, dict):
        keys = [key for key in _COPILOT_REJECTED_TELEMETRY_KEYS if key in telemetry]
        if keys:
            names = ", ".join(f"telemetry.{key}" for key in keys)
            return f"{path} sets {names}, which the app only accepts from managed settings"
    return None


def _seat_relay_warning(provider: str) -> str:
    return (
        f"The GitHub Copilot app isn't set up to route its GitHub Copilot models through "
        f"{provider}. Run `ug configure --agents copilot` again."
    )


def _uses_seat_relay(state: dict, provider: str) -> bool:
    """Whether ``provider`` relays a GitHub Copilot seat. The app's own GitHub Copilot models then
    go through ug's relay in the plugin's extension, and the app gets no Databricks rows for it:
    the relay's swap header needs a fresh value per request, and the app's provider headers are
    fixed."""
    from ucode.agents.copilot import uses_seat_relay

    return uses_seat_relay(state, provider, tool="vscode")


def _managed_default_display_name(
    state: dict, catalog: list[CopilotModel], desired: copilot_app.DesiredProviders
) -> str | None:
    """The app's name for the workspace's managed default model, when the app has a row for it.
    ug doesn't set the app's default: the app keeps that per user."""
    from ucode.agents.vscode import managed_default_model

    if not (default := managed_default_model(state, catalog)):
        return None
    for app_provider in desired.app:
        for row in app_provider.models:
            if row["wireModel"] == default:
                return row["displayName"]
    return None


def configure_copilot_app(state: dict, *, provider: str | None, location: str | None) -> None:
    """Install ug's plugin in the app with the models the Copilot CLI was configured with.

    Problems only warn: the app never fails configure."""
    if is_dry_run():
        print_note(_COPILOT_APP_DRY_RUN_NOTE)
        return
    _cleanup_copilot_app_leftover()
    state.pop(_LEGACY_WINDOWS_TRACING_KEY, None)
    remove_legacy_copilot_app_tracing()
    from ucode.agents.vscode import copilot_models

    try:
        state = _with_expanded_sources(copilot_app_state(state))
        relayed = bool(provider) and _uses_seat_relay(state, provider)
        catalog = (
            []
            if relayed
            else copilot_models(state, provider, location, agent="the GitHub Copilot app")
        )
    except RuntimeError as exc:
        print_warning(f"Skipped the GitHub Copilot app: {exc}")
        return
    desired = copilot_app.desired_providers(
        state["workspace"], catalog, provider_service=None if relayed else provider
    )
    try:
        plugin_config = copilot_app_plugin_config(
            state, desired, seat_relay=provider if relayed else None
        )
    except RuntimeError as exc:
        print_warning(f"Skipped the GitHub Copilot app: {exc}")
        return
    try:
        copilot_app_plugin.install(plugin_config)
    except copilot_app_plugin.SettingsError as exc:
        print_warning(
            f"Skipped the GitHub Copilot app: {exc}, and ug doesn't rewrite a settings file it "
            "can't read. Fix it, then run `ug configure --agents copilot` again."
        )
        return
    # ug's file writers raise RuntimeError for a failed write.
    except (OSError, RuntimeError) as exc:
        print_warning(f"Could not install ug's plugin in the GitHub Copilot app: {exc}")
        return
    from ucode.databricks import databricks_cli_path
    from ucode.state import record_databricks_cli

    if os.path.isabs(cli := databricks_cli_path()):
        try:
            record_databricks_cli(cli)
        except RuntimeError as exc:
            print_warning(str(exc))
    if reason := copilot_settings_ignored_reason():
        print_warning(
            f"The GitHub Copilot app ignores its whole settings file while {reason}, so it won't "
            "load ug's plugin. Remove those keys."
        )
    if relayed:
        print_kv("GitHub Copilot app", f"installed ug's plugin, routing Copilot through {provider}")
        print_note(
            "In the GitHub Copilot app: open it (or restart it), then start a chat and pick a "
            "GitHub Copilot model."
        )
        return
    print_kv("GitHub Copilot app", f"installed ug's plugin with {len(catalog)} Databricks models")
    print_note(
        "In the GitHub Copilot app: open it (or restart it), then open the model picker and choose "
        "a Databricks model."
    )
    if name := _managed_default_display_name(state, catalog, desired):
        print_note(
            f"To start new chats on {name}, pick it in the GitHub Copilot app; Settings -> "
            "Sessions -> Default model makes it stick."
        )


# Saved by earlier builds' `--copilot-app-windows-tracing`.
_LEGACY_WINDOWS_TRACING_KEY = "copilot_app_windows_tracing"


def remove_legacy_copilot_app_tracing() -> bool:
    """Remove what earlier builds wrote for the app's own trace export. Problems only warn."""
    if is_dry_run():
        return False
    try:
        return copilot_app_tracing.remove_legacy()
    except (copilot_app_plugin.SettingsError, OSError, RuntimeError) as exc:
        print_warning(f"Could not remove ug's earlier GitHub Copilot app tracing setup: {exc}")
        return False


COPILOT_APP_REMOVAL_PENDING = (
    "removal pending: the Databricks models are removed the next time the GitHub Copilot app starts"
)


def revert_copilot_app() -> str:
    """Remove ug's providers from the running app, then ug's plugin. With the app closed, leave
    both to the plugin's next start. Never starts the app. Returns a summary for revert."""
    if is_dry_run():
        print_note(_COPILOT_APP_DRY_RUN_NOTE)
        return COPILOT_APP_DRY_RUN
    # First, so a settings file ug created is empty once the plugin's keys go too.
    legacy_removed = remove_legacy_copilot_app_tracing()
    result = _revert_copilot_app_plugin()
    return "removed" if legacy_removed and result == "not set up" else result


def _revert_copilot_app_plugin() -> str:
    _cleanup_copilot_app_leftover()
    had_plugin = copilot_app_plugin.plugin_root().exists()
    if not had_plugin and not copilot_app_installed():
        return "not set up"
    # Marked first, so no extension writes the rows back while ug deletes them. With the app
    # closed, the same mark has the plugin delete them on the app's next start.
    marked = False
    if had_plugin:
        try:
            marked = copilot_app_plugin.defer_remove()
        except copilot_app_plugin.SettingsError as exc:
            return f"failed: {exc}; remove ug's databricks-unity-gateway entries from it yourself"
    try:
        with copilot_app_plugin.sync_lock() if marked else nullcontext():
            unregistered = copilot_app.unregister()
    except copilot_app.CopilotAppNotRunning:
        if not had_plugin:
            return "not set up"
        if marked:
            return COPILOT_APP_REMOVAL_PENDING
        # Without an installed plugin nothing can remove the rows later.
        try:
            copilot_app_plugin.remove()
        except copilot_app_plugin.SettingsError as exc:
            return f"failed: {exc}; remove ug's databricks-unity-gateway entries from it yourself"
        return (
            "removed ug's plugin; open the GitHub Copilot app and run `ug revert` again to remove "
            "the Databricks models from it"
        )
    except copilot_app.CopilotAppError as exc:
        return f"failed: {exc}"
    try:
        plugin_removed = copilot_app_plugin.remove()
    except copilot_app_plugin.SettingsError as exc:
        if not had_plugin:
            return "removed" if unregistered else "not set up"
        return f"failed: {exc}; remove ug's databricks-unity-gateway entries from it yourself"
    return "removed" if unregistered or plugin_removed else "not set up"


def _cleanup_copilot_app_leftover() -> None:
    try:
        copilot_app_plugin.cleanup_leftover()
    except OSError as exc:
        print_warning(f"Could not delete ug's old GitHub Copilot app plugin: {exc}")


@dataclass(frozen=True)
class CopilotAppStatus:
    """The app's row in ``ug status``, and what to tell the user when its turns can't work."""

    row: str
    warnings: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


_COPILOT_PLUGIN_LABELS = {
    "installed": "installed",
    "removal_pending": "removal pending",
    "disabled": "disabled",
    "not_installed": "not installed",
    "invalid_settings": "invalid settings",
}
_COPILOT_PLUGIN_PROBLEMS = {
    "disabled": "is turned off",
    "not_installed": "is missing",
    "invalid_settings": "isn't loaded, since Copilot's settings.json isn't valid JSON",
}
_COPILOT_REGISTRATION_LABELS = {
    "registered": "registered",
    "outdated": "out of date",
    "not_registered": "not registered",
    "not_running": "unknown (app closed)",
}


def copilot_app_unconfigured_status() -> str | None:
    """The app's row in ``ug status`` once Copilot is no longer configured: a revert with the app
    closed is still pending until the plugin's next start."""
    _cleanup_copilot_app_leftover()
    if copilot_app_plugin.status() == "removal_pending":
        return COPILOT_APP_REMOVAL_PENDING
    return None


def copilot_app_status(
    state: dict, *, provider: str | None, location: str | None
) -> CopilotAppStatus | None:
    """The app's row in ``ug status`` for ``state`` (managed settings already layered on), or None
    when ug doesn't set the app up."""
    _cleanup_copilot_app_leftover()
    plugin_status = copilot_app_plugin.status()
    if plugin_status == "not_installed" and not copilot_app_installed():
        return None
    if plugin_status == "removal_pending":
        return CopilotAppStatus(COPILOT_APP_REMOVAL_PENDING)
    warnings: list[str] = []
    notes: list[str] = []
    try:
        if provider and _uses_seat_relay(state, provider):
            if copilot_app_plugin.seat_relay_provider() == provider:
                registration = f"through {provider}"
            else:
                registration = "not through the seat relay"
                warnings.append(_seat_relay_warning(provider))
            if diagnostics := copilot_app_plugin.debug_log_path():
                notes.append(f"Diagnostics: {diagnostics}")
        else:
            desired = copilot_app.state_providers(
                _with_expanded_sources(state), provider=provider, location=location
            )
            registration = copilot_app.status(desired.app)
    except copilot_app.CopilotAppError as exc:
        registration = f"unknown ({exc})"
    except RuntimeError:
        registration = "unknown (no models to compare)"
    plugin_label = _COPILOT_PLUGIN_LABELS[plugin_status]
    if plugin_status == "installed" and (moved := copilot_app_plugin.missing_ug_path()):
        warnings.append(
            f"ug's plugin for the GitHub Copilot app runs ug from {moved}, which no longer exists, "
            "so its turns fail. Run `ug configure --agents copilot` again."
        )
    ignored = copilot_settings_ignored_reason() if plugin_status != "invalid_settings" else None
    if ignored:
        plugin_label += ", ignored"
        warnings.append(
            f"The GitHub Copilot app ignores its whole settings file while {ignored}, so it "
            "doesn't load ug's plugin. Remove those keys."
        )
    problem = _COPILOT_PLUGIN_PROBLEMS.get(plugin_status) or ("isn't loaded" if ignored else None)
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
    row = f"plugin {plugin_label}, models {_COPILOT_REGISTRATION_LABELS.get(registration, registration)}"
    return CopilotAppStatus(row, tuple(warnings), tuple(notes))
