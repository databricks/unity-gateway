"""The Databricks plugin for the GitHub Copilot desktop app.

ug writes a local plugin marketplace it owns and turns it on in Copilot's ``settings.json``. The
plugin's extension writes ug's providers and model rows to the app's model picker over the app's
local WebSocket API, serves them straight from the AI Gateway under the same ids, and mints a fresh
Databricks token for every request with ``ug auth-token``.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ucode.config_io import atomic_write_json

MARKETPLACE = "unity-gateway"
PLUGIN = "databricks"
PLUGIN_KEY = f"{PLUGIN}@{MARKETPLACE}"
EXTENSION = "databricks"
VERSION = "0.1.0"

Wire = Literal["anthropic", "responses", "completions"]
Status = Literal["installed", "disabled", "not_installed", "invalid_settings"]

_MARKETPLACES_KEY = "extraKnownMarketplaces"
_ENABLED_KEY = "enabledPlugins"
_SOURCE_FILES = ("extension.mjs", "ug-plugin.mjs", "app-rows.mjs")
# The app unloads the extension within about 3 s of its settings keys going away. Deleting the
# directory first leaves the running extensions serving open chats until a new chat starts.
UNLOAD_WAIT_S = 5.0
# The runtime asks for 32K output tokens and fails a turn on a model that advertises fewer.
MIN_OUTPUT_TOKENS = 32_000


class SettingsError(RuntimeError):
    """Copilot's ``settings.json`` can't be safely rewritten."""


@dataclass(frozen=True)
class PluginModel:
    """A model the extension serves. The session uses these fields instead of the picker row's,
    so they must repeat its name, limits and vision support."""

    model_id: str
    wire_model: str
    name: str
    # (max prompt tokens, max output tokens); None keeps the runtime's defaults.
    limits: tuple[int, int] | None = None
    vision: bool = False
    # False stops the runtime from sending `reasoning_effort`; None keeps its default.
    reasoning_effort: bool | None = None


@dataclass(frozen=True)
class PluginProvider:
    """One provider the extension serves. ``id`` must match the provider ug registers in the app,
    and each model's ``model_id`` the picker row, or the app's rows stop working."""

    id: str
    wire: Wire
    base_path: str
    models: tuple[PluginModel, ...]


@dataclass(frozen=True)
class AppProviderRows:
    """A provider and its model rows as the app's WebSocket API takes them. The extension writes
    them to the app's model picker."""

    provider: Mapping[str, Any]
    secret: Mapping[str, str] | None
    models: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class PluginConfig:
    workspace: str
    ug_path: str
    providers: tuple[PluginProvider, ...]
    headers: Mapping[str, str] = field(default_factory=dict)
    profile: str | None = None
    # The extension's process gets the app runtime's PATH, which may lack the directories ug and
    # the Databricks CLI live in.
    path_dirs: tuple[str, ...] = ()
    # `ug auth-token` runs the Databricks CLI, so its directory goes on the PATH too.
    databricks_path: str | None = None
    app_providers: tuple[AppProviderRows, ...] = ()
    # Every provider id ug may have written to the app; the extension deletes those not in
    # ``app_providers``.
    owned_provider_ids: tuple[str, ...] = ()


def copilot_settings_path() -> Path:
    copilot_home = os.environ.get("COPILOT_HOME")
    base = Path(copilot_home) if copilot_home else Path.home() / ".copilot"
    return base / "settings.json"


def plugin_root() -> Path:
    return Path.home() / ".ucode" / "copilot-app-plugin"


def _plugin_dir(root: Path) -> Path:
    return root / "plugins" / PLUGIN


def _extension_dir(root: Path) -> Path:
    return _plugin_dir(root) / "extensions" / EXTENSION


def _token_command(config: PluginConfig) -> list[str]:
    command = [config.ug_path, "auth-token", "--host", config.workspace]
    if config.profile:
        command += ["--profile", config.profile]
    return command


def _login_hint(config: PluginConfig) -> str:
    login = f"databricks auth login --host {config.workspace}"
    if config.profile:
        login += f" --profile {config.profile}"
    return f"Couldn't get a Databricks token for {config.workspace}. Run `{login}`, then try again."


def _provider_entry(provider: PluginProvider, config: PluginConfig) -> dict:
    entry: dict = {
        "name": provider.id,
        "type": "anthropic" if provider.wire == "anthropic" else "openai",
        "baseUrl": config.workspace.rstrip("/") + provider.base_path,
        "headers": dict(config.headers),
    }
    if provider.wire != "anthropic":
        entry["wireApi"] = provider.wire
    return entry


def _model_entry(model: PluginModel, provider: PluginProvider) -> dict:
    entry: dict = {
        "id": model.model_id,
        "modelId": model.model_id,
        "provider": provider.id,
        "wireModel": model.wire_model,
        "name": model.name,
    }
    if model.limits:
        prompt, output = model.limits
        entry["maxPromptTokens"] = prompt
        if output >= MIN_OUTPUT_TOKENS:
            entry["maxOutputTokens"] = output
            entry["maxContextWindowTokens"] = prompt + output
    supports: dict = {}
    if model.vision:
        supports["vision"] = True
    if model.reasoning_effort is not None:
        supports["reasoningEffort"] = model.reasoning_effort
    if supports:
        entry["capabilities"] = {"supports": supports}
    return entry


def _app_entry(rows: AppProviderRows) -> dict:
    return {
        "provider": dict(rows.provider),
        "secret": dict(rows.secret) if rows.secret else None,
        "models": [dict(model) for model in rows.models],
    }


def extension_config(config: PluginConfig) -> dict:
    """The JSON the extension reads at start."""
    return {
        "providers": [_provider_entry(provider, config) for provider in config.providers],
        "models": [
            _model_entry(model, provider)
            for provider in config.providers
            for model in provider.models
        ],
        "tokenCommand": _token_command(config),
        "pathDirs": list(config.path_dirs),
        "databricksPath": config.databricks_path,
        "loginHint": _login_hint(config),
        "app": {
            "providers": [_app_entry(rows) for rows in config.app_providers],
            "ownedProviderIds": list(config.owned_provider_ids),
        },
    }


def write_plugin(config: PluginConfig, root: Path | None = None) -> Path:
    """Write the marketplace, the plugin and its extension under ``root``; return ``root``."""
    root = root or plugin_root()
    description = "Databricks models in the GitHub Copilot app, through Unity Gateway"
    atomic_write_json(
        root / ".github" / "plugin" / "marketplace.json",
        {
            "name": MARKETPLACE,
            "owner": {"name": "Unity Gateway"},
            "plugins": [
                {
                    "name": PLUGIN,
                    "source": f"./plugins/{PLUGIN}",
                    "version": VERSION,
                    "description": description,
                }
            ],
        },
    )
    atomic_write_json(
        _plugin_dir(root) / ".plugin" / "plugin.json",
        {
            "name": PLUGIN,
            "version": VERSION,
            "description": description,
            "author": {"name": "Unity Gateway"},
        },
    )
    extension_dir = _extension_dir(root)
    extension_dir.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).parent
    for name in _SOURCE_FILES:
        shutil.copyfile(source / name, extension_dir / name)
    atomic_write_json(extension_dir / "config.json", extension_config(config))
    return root


def read_settings(path: Path | None = None) -> dict:
    """Copilot's ``settings.json`` (``{}`` when absent). Raises ``SettingsError`` when the file
    isn't a JSON object: Copilot then ignores the whole file, so ug must not rewrite it."""
    path = path or copilot_settings_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SettingsError(f"{path} is not valid JSON") from exc
    if not isinstance(data, dict):
        raise SettingsError(f"{path} is not a JSON object")
    return data


def settings_valid(path: Path | None = None) -> bool:
    try:
        read_settings(path)
    except SettingsError:
        return False
    return True


def _object(settings: dict, key: str, path: Path) -> dict:
    value = settings.setdefault(key, {})
    if not isinstance(value, dict):
        raise SettingsError(f"{path}: {key} is not a JSON object")
    return value


def enable(root: Path | None = None, settings_path: Path | None = None) -> None:
    """Add ug's marketplace and turn its plugin on, keeping every other setting."""
    root = root or plugin_root()
    path = settings_path or copilot_settings_path()
    settings = read_settings(path)
    _object(settings, _MARKETPLACES_KEY, path)[MARKETPLACE] = {
        "source": {"source": "directory", "path": str(root)}
    }
    _object(settings, _ENABLED_KEY, path)[PLUGIN_KEY] = True
    atomic_write_json(path, settings)


def install(
    config: PluginConfig, root: Path | None = None, settings_path: Path | None = None
) -> Path:
    path = settings_path or copilot_settings_path()
    read_settings(path)
    root = write_plugin(config, root)
    enable(root, path)
    return root


def remove(
    root: Path | None = None,
    settings_path: Path | None = None,
    wait: Callable[[float], None] | None = None,
) -> bool:
    """Remove ug's settings keys, give the app time to unload the extension, then remove the
    plugin directory. Returns whether anything was removed."""
    root = root or plugin_root()
    path = settings_path or copilot_settings_path()
    settings = read_settings(path)
    changed = False
    for key, entry in ((_MARKETPLACES_KEY, MARKETPLACE), (_ENABLED_KEY, PLUGIN_KEY)):
        value = settings.get(key)
        if isinstance(value, dict) and entry in value:
            del value[entry]
            if not value:
                del settings[key]
            changed = True
    if changed:
        atomic_write_json(path, settings)
    if root.exists():
        if changed:
            (wait or time.sleep)(UNLOAD_WAIT_S)
        shutil.rmtree(root)
        changed = True
    return changed


def status(root: Path | None = None, settings_path: Path | None = None) -> Status:
    root = root or plugin_root()
    try:
        settings = read_settings(settings_path)
    except SettingsError:
        return "invalid_settings"
    marketplaces = settings.get(_MARKETPLACES_KEY)
    marketplace = marketplaces.get(MARKETPLACE) if isinstance(marketplaces, dict) else None
    source = marketplace.get("source") if isinstance(marketplace, dict) else None
    if not (
        isinstance(source, dict)
        and source.get("path") == str(root)
        and (_extension_dir(root) / "extension.mjs").exists()
    ):
        return "not_installed"
    enabled = settings.get(_ENABLED_KEY)
    if isinstance(enabled, dict) and enabled.get(PLUGIN_KEY) is True:
        return "installed"
    return "disabled"
