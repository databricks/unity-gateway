"""The Databricks plugin for the GitHub Copilot desktop app.

ug writes a local plugin marketplace it owns and turns it on in Copilot's ``settings.json``. The
plugin's extension writes ug's providers and model rows to the app's model picker over the app's
local WebSocket API and serves them straight from the AI Gateway under the same ids. It gets each
request's Databricks token from ``ug auth-token``, reusing it until shortly before it expires.
The app's window loads the picker's providers only when it starts, so after a sync that changed
ug's rows, the extension restarts the app once, when no chat is running a turn.

With the app closed, ug can't delete its rows, so ``defer_remove`` leaves that and the settings keys
to the extension's next start, and ``cleanup_leftover`` deletes the directory afterwards.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ucode.config_io import atomic_write_json
from ucode.custom_oauth import CustomOAuthConfig, build_custom_auth_token_argv
from ucode.databricks import build_auth_token_argv

MARKETPLACE = "databricks-unity-gateway"
PLUGIN = "unity-gateway"
PLUGIN_KEY = f"{PLUGIN}@{MARKETPLACE}"
# Earlier builds' ids. Configure moves them to the current ones, and removal takes out both.
_LEGACY_MARKETPLACE = "unity-gateway"
_LEGACY_PLUGIN = "databricks"
_IDS = ((MARKETPLACE, PLUGIN), (_LEGACY_MARKETPLACE, _LEGACY_PLUGIN))
EXTENSION = "databricks"
VERSION = "0.1.0"

Wire = Literal["anthropic", "responses", "completions"]
Status = Literal["installed", "removal_pending", "disabled", "not_installed", "invalid_settings"]

_MARKETPLACES_KEY = "extraKnownMarketplaces"
_ENABLED_KEY = "enabledPlugins"
_TELEMETRY_KEY = "telemetry"
_HEADERS_PROVIDER_KEY = "headersProvider"
_UG_BINARY_NAMES = frozenset({"ug", "ug.exe", "ucode", "ucode.exe"})
_SOURCE_FILES = (
    "extension.mjs",
    "ug-plugin.mjs",
    "app-rows.mjs",
    "app-remove.mjs",
    "app-restart.mjs",
    "app-state.mjs",
    "app-host.mjs",
    "app-proxy.mjs",
    "app-windows.mjs",
    "app-debug.mjs",
    "app-system-proxy.mjs",
    "app-seat-relay.mjs",
    "app-seat-redirect.mjs",
)
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
    use_pat: bool = False
    custom_oauth: CustomOAuthConfig | None = None
    # The extension's process gets the app runtime's PATH, which may lack the directories ug and
    # the Databricks CLI live in.
    path_dirs: tuple[str, ...] = ()
    # `ug auth-token` runs the Databricks CLI, so its directory goes on the PATH too.
    databricks_path: str | None = None
    app_providers: tuple[AppProviderRows, ...] = ()
    # Every provider id ug may have written to the app; the extension deletes those not in
    # ``app_providers``.
    owned_provider_ids: tuple[str, ...] = ()
    # The relayed Model Provider Service the app's own GitHub Copilot calls go through; None
    # leaves them direct.
    seat_relay: str | None = None
    # ug's own User-Agent (`ucode/<v> (<platform>)`); the relay puts the runtime's product tokens
    # between the two, so the gateway attributes the call to Copilot at the runtime's version.
    user_agent: str | None = None


def copilot_settings_path() -> Path:
    copilot_home = os.environ.get("COPILOT_HOME")
    base = Path(copilot_home) if copilot_home else Path.home() / ".copilot"
    return base / "settings.json"


def plugin_root() -> Path:
    return Path.home() / ".ucode" / "copilot-app-plugin"


def _plugin_dir(root: Path, plugin: str = PLUGIN) -> Path:
    return root / "plugins" / plugin


def _extension_dir(root: Path, plugin: str = PLUGIN) -> Path:
    return _plugin_dir(root, plugin) / "extensions" / EXTENSION


def _token_command(config: PluginConfig) -> list[str]:
    # `ug auth-token --host` ignores the saved PAT and custom OAuth choices, so the command names
    # them itself.
    if config.custom_oauth:
        command = build_custom_auth_token_argv(config.workspace, config.custom_oauth)
    else:
        command = build_auth_token_argv(config.workspace, config.profile, use_pat=config.use_pat)
    return [config.ug_path, *command[1:]]


def _login_hint(config: PluginConfig) -> str:
    failed = f"Couldn't get a Databricks token for {config.workspace}."
    if config.use_pat:
        section = config.profile or "your-profile"
        return (
            f"{failed} Add a `token = <PAT>` entry under [{section}] in ~/.databrickscfg, or rerun "
            "`ug configure`, then try again."
        )
    if config.custom_oauth:
        return f"{failed} Rerun `ug configure` to sign in again, then try again."
    login = f"databricks auth login --host {config.workspace}"
    if config.profile:
        login += f" --profile {config.profile}"
    return f"{failed} Run `{login}`, then try again."


def _missing_ug_hint(config: PluginConfig) -> str:
    return f"Unity Gateway wasn't found at {config.ug_path}. Rerun `ug configure`, then try again."


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
        "ugPath": config.ug_path,
        "pathDirs": list(config.path_dirs),
        "databricksPath": config.databricks_path,
        "loginHint": _login_hint(config),
        "missingUgHint": _missing_ug_hint(config),
        "app": {
            "providers": [_app_entry(rows) for rows in config.app_providers],
            "ownedProviderIds": list(config.owned_provider_ids),
        },
        "seatRelay": (
            {
                "gatewayBase": config.workspace.rstrip("/"),
                "providerService": config.seat_relay,
                "headers": dict(config.headers),
                "userAgent": config.user_agent,
                "askGitHub": True,
            }
            if config.seat_relay
            else None
        ),
    }


# What the extension keeps between runs, in its own directory; a reinstall carries them over, so
# an unchanged configure neither resyncs nor restarts the app.
_EXTENSION_STATE_FILES = (
    "app-sync.json",
    "app-restart.json",
    "app-restart.log",
    "seat-relay-sessions.json",
    "seat-relay-requests.json",
    "seat-relay-debug.log",
    "seat-relay-debug.log.1",
)
_STAGING_PREFIX = ".staging-"
_REPLACED_PREFIX = ".replaced-"
# Older leftovers are from a run that crashed, never from one still writing.
_LEFTOVER_AGE_S = 600.0


def _write_tree(config: PluginConfig, root: Path) -> None:
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


def _carry_over(old: Path, new: Path) -> None:
    for name in _EXTENSION_STATE_FILES:
        if (_extension_dir(old) / name).is_file():
            shutil.copy2(_extension_dir(old) / name, _extension_dir(new) / name)
    if (marker := _created_settings_marker(old)).is_file():
        shutil.copy2(marker, _created_settings_marker(new))
    # Settings may still name an earlier build's copy until ``enable`` moves them on.
    if (legacy := _plugin_dir(old, _LEGACY_PLUGIN)).is_dir():
        shutil.copytree(legacy, _plugin_dir(new, _LEGACY_PLUGIN))


def _fsync_tree(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file():
            # Best effort: Windows syncs only a handle opened for writing.
            try:
                with path.open("rb+") as file:
                    os.fsync(file.fileno())
            except OSError:
                pass


def _remove_leftovers(root: Path) -> None:
    now = time.time()
    for prefix in (_STAGING_PREFIX, _REPLACED_PREFIX):
        for leftover in root.parent.glob(f".{root.name}{prefix}*"):
            try:
                if now - leftover.stat().st_mtime > _LEFTOVER_AGE_S:
                    shutil.rmtree(leftover)
            except OSError:
                pass


def _publish(staged: Path, root: Path) -> None:
    """Move ``staged`` to ``root`` with renames only, so the app never reads a half-written plugin.
    Windows can't rename onto an existing directory, so the old one is moved aside first."""
    replaced = root.with_name(f".{root.name}{_REPLACED_PREFIX}{uuid.uuid4().hex}")
    had_old = root.exists()
    if had_old:
        os.replace(root, replaced)
    try:
        os.replace(staged, root)
    except OSError:
        if had_old:
            os.replace(replaced, root)
        raise
    if had_old:
        shutil.rmtree(replaced, ignore_errors=True)


def write_plugin(config: PluginConfig, root: Path | None = None) -> Path:
    """Write the marketplace, the plugin and its extension to ``root``; return ``root``. The whole
    tree is written next to it first and then swapped in."""
    root = root or plugin_root()
    root.parent.mkdir(parents=True, exist_ok=True)
    _remove_leftovers(root)
    staged = root.with_name(f".{root.name}{_STAGING_PREFIX}{uuid.uuid4().hex}")
    try:
        _write_tree(config, staged)
        if root.is_dir():
            _carry_over(root, staged)
        _fsync_tree(staged)
        _publish(staged, root)
    finally:
        shutil.rmtree(staged, ignore_errors=True)
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


def _check_writable(path: Path) -> None:
    if path.is_symlink():
        raise SettingsError(
            f"{path} is a symlink, which ug won't replace. Edit the file it points to instead."
        )


def _write_settings(path: Path, settings: dict) -> None:
    _check_writable(path)
    mode = path.stat().st_mode & 0o777 if path.exists() else None
    atomic_write_json(path, settings)
    if mode is not None:
        path.chmod(mode)


def _owns_marketplace(settings: dict, root: Path, marketplace: str = MARKETPLACE) -> bool:
    marketplaces = settings.get(_MARKETPLACES_KEY)
    entry = marketplaces.get(marketplace) if isinstance(marketplaces, dict) else None
    source = entry.get("source") if isinstance(entry, dict) else None
    return isinstance(source, dict) and source.get("path") == str(root)


def _installed_id(settings: dict, root: Path) -> tuple[str, str] | None:
    """The marketplace and plugin of ug's installed plugin, under its current or an earlier id."""
    for marketplace, plugin in _IDS:
        if (
            _owns_marketplace(settings, root, marketplace)
            and (_extension_dir(root, plugin) / "extension.mjs").exists()
        ):
            return marketplace, plugin
    return None


def _installed(settings: dict, root: Path) -> bool:
    return _installed_id(settings, root) is not None


def _ug_entries(settings: dict, root: Path, ids=_IDS) -> list[tuple[str, str]]:
    """ug's entries in ``settings`` under ``ids``. A marketplace of that name at another path isn't
    ug's, so it and its plugin key are left alone."""
    entries = []
    for marketplace, plugin in ids:
        plugin_key = f"{plugin}@{marketplace}"
        marketplaces = settings.get(_MARKETPLACES_KEY)
        if (
            isinstance(marketplaces, dict)
            and marketplace in marketplaces
            and not _owns_marketplace(settings, root, marketplace)
        ):
            continue
        for key, entry in ((_MARKETPLACES_KEY, marketplace), (_ENABLED_KEY, plugin_key)):
            value = settings.get(key)
            if isinstance(value, dict) and entry in value:
                entries.append((key, entry))
    return entries


def _drop_entries(settings: dict, entries: list[tuple[str, str]]) -> None:
    for key, entry in entries:
        del settings[key][entry]
        if not settings[key]:
            del settings[key]


def _has_ug_keys(settings: dict, root: Path) -> bool:
    return bool(_ug_entries(settings, root))


def debug_log_path(root: Path | None = None) -> Path | None:
    """The installed extension's diagnostics log, when it has written one."""
    path = _extension_dir(root or plugin_root()) / "seat-relay-debug.log"
    return path if path.is_file() else None


def missing_ug_path(root: Path | None = None) -> str | None:
    """The absolute ``ug`` path the installed extension runs, when it no longer exists there."""
    config = _read_extension_config(root or plugin_root())
    path = config.get("ugPath") if config is not None else None
    if isinstance(path, str) and os.path.isabs(path) and not os.path.isfile(path):
        return path
    return None


def _read_extension_config(root: Path, plugin: str = PLUGIN) -> dict | None:
    try:
        path = _extension_dir(root, plugin) / "config.json"
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return config if isinstance(config, dict) else None


def _removal_requested(root: Path) -> bool:
    configs = (_read_extension_config(root, plugin) for _marketplace, plugin in _IDS)
    return any(config is not None and config.get("remove") is True for config in configs)


def enable(root: Path | None = None, settings_path: Path | None = None) -> None:
    """Add ug's marketplace and turn its plugin on, keeping every other setting."""
    root = root or plugin_root()
    path = settings_path or copilot_settings_path()
    settings = read_settings(path)
    _drop_entries(settings, _ug_entries(settings, root, _IDS[1:]))
    _object(settings, _MARKETPLACES_KEY, path)[MARKETPLACE] = {
        "source": {"source": "directory", "path": str(root)}
    }
    _object(settings, _ENABLED_KEY, path)[PLUGIN_KEY] = True
    _write_settings(path, settings)


def seat_relay_provider(root: Path | None = None) -> str | None:
    """The relayed Model Provider Service the installed extension routes the app's GitHub Copilot
    calls through, or None."""
    config = _read_extension_config(root or plugin_root())
    relay = config.get("seatRelay") if config is not None else None
    service = relay.get("providerService") if isinstance(relay, dict) else None
    return service if isinstance(service, str) and service else None


def remove_headers_provider(settings_path: Path | None = None) -> bool:
    """Remove the ``telemetry.headersProvider`` running ``ug otel-headers`` that earlier builds
    wrote; another one stays. Returns whether it was removed."""
    path = settings_path or copilot_settings_path()
    if not path.exists():
        return False
    settings = read_settings(path)
    telemetry = settings.get(_TELEMETRY_KEY)
    provider = telemetry.get(_HEADERS_PROVIDER_KEY) if isinstance(telemetry, dict) else None
    args = provider.get("args") if isinstance(provider, dict) else None
    executable = provider.get("path") if isinstance(provider, dict) else None
    # ug wrote ``<ug> otel-headers --host <workspace> ...``; a user's own helper stays, even one
    # with the same arguments.
    if (
        not isinstance(telemetry, dict)
        or not isinstance(executable, str)
        or Path(executable).name.lower() not in _UG_BINARY_NAMES
        or not isinstance(args, list)
        or args[:1] != ["otel-headers"]
        or "--host" not in args
    ):
        return False
    del telemetry[_HEADERS_PROVIDER_KEY]
    if not telemetry:
        del settings[_TELEMETRY_KEY]
    _write_settings(path, settings)
    return True


def _created_settings_marker(root: Path) -> Path:
    return root / "created-settings"


def _created_settings(root: Path, path: Path) -> bool:
    """Whether ug created ``path`` when it installed the plugin under ``root``."""
    try:
        return _created_settings_marker(root).read_text(encoding="utf-8") == str(path)
    except OSError:
        return False


def _drop_settings(root: Path, path: Path, settings: dict) -> bool:
    """Delete ``path`` when ug created it and nothing else is left in it."""
    if settings or not _created_settings(root, path):
        return False
    path.unlink(missing_ok=True)
    return True


def install(
    config: PluginConfig, root: Path | None = None, settings_path: Path | None = None
) -> Path:
    path = settings_path or copilot_settings_path()
    settings = read_settings(path)
    # Checked before anything is written, so a refusal leaves no plugin behind.
    for key in (_MARKETPLACES_KEY, _ENABLED_KEY):
        _object(dict(settings), key, path)
    marketplaces = settings.get(_MARKETPLACES_KEY) or {}
    if MARKETPLACE in marketplaces and not _owns_marketplace(settings, root or plugin_root()):
        raise SettingsError(
            f"{path}: {_MARKETPLACES_KEY}.{MARKETPLACE} already names another plugin marketplace"
        )
    _check_writable(path)
    created = not path.exists()
    root = write_plugin(config, root)
    if created:
        _created_settings_marker(root).write_text(str(path), encoding="utf-8")
    enable(root, path)
    # An earlier build's copy, which the settings no longer name.
    shutil.rmtree(_plugin_dir(root, _LEGACY_PLUGIN), ignore_errors=True)
    return root


def remove(
    root: Path | None = None,
    settings_path: Path | None = None,
    wait: Callable[[float], None] | None = None,
) -> bool:
    """Remove ug's settings keys, give the app time to unload the extension, then remove the
    plugin directory, under the current and earlier ids. A marketplace of ug's name at another
    path isn't ug's, so it and its plugin key stay. Returns whether anything was removed."""
    root = root or plugin_root()
    path = settings_path or copilot_settings_path()
    settings = read_settings(path)
    entries = _ug_entries(settings, root)
    _drop_entries(settings, entries)
    changed = bool(entries)
    if changed and not _drop_settings(root, path, settings):
        _write_settings(path, settings)
    if root.exists():
        if changed:
            (wait or time.sleep)(UNLOAD_WAIT_S)
        shutil.rmtree(root)
        changed = True
    return changed


_SYNC_LOCK_FILE = "app-sync.lock"
# As app-rows.mjs's LOCK_STALE_MS: an older lock is from an extension that died holding it.
_SYNC_LOCK_STALE_S = 60.0


def _take_sync_lock(path: Path, owner: str) -> bool:
    try:
        with path.open("x", encoding="utf-8") as file:
            file.write(owner)
        return True
    except FileExistsError:
        pass
    try:
        stale = path.read_text(encoding="utf-8")
        if time.time() - path.stat().st_mtime < _SYNC_LOCK_STALE_S:
            return False
        # Only one process can move a stale lock away; a lock replaced meanwhile goes back.
        moved = path.with_name(f"{path.name}.{owner}")
        os.replace(path, moved)
        if moved.read_text(encoding="utf-8") != stale:
            os.replace(moved, path)
            return False
        moved.unlink()
        with path.open("x", encoding="utf-8") as file:
            file.write(owner)
        return True
    except OSError:
        return False


@contextmanager
def sync_lock(
    root: Path | None = None,
    *,
    timeout: float = 20.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[bool]:
    """Hold the lock the plugin's extensions sync and remove under, so none writes the app's rows
    meanwhile. Yields whether it got the lock within ``timeout``; an extension still syncing then
    stops on its own once the plugin is marked for removal."""
    directory = _extension_dir(root or plugin_root())
    if not directory.is_dir():
        yield False
        return
    path = directory / _SYNC_LOCK_FILE
    owner = f"ug-{os.getpid()}-{uuid.uuid4().hex}"
    deadline = clock() + timeout
    while not (held := _take_sync_lock(path, owner)) and clock() < deadline:
        sleep(0.2)
    try:
        yield held
    finally:
        if held:
            try:
                if path.read_text(encoding="utf-8") == owner:
                    path.unlink()
            except OSError:
                pass


def defer_remove(root: Path | None = None, settings_path: Path | None = None) -> bool:
    """Ask the extension to remove ug's rows from the app and then ug's settings keys the next time
    it starts, for when the app isn't running. The plugin stays enabled so that the extension runs.
    Returns whether there was an installed plugin to mark."""
    root = root or plugin_root()
    path = settings_path or copilot_settings_path()
    settings = read_settings(path)
    # The extension rewrites settings.json under the same rules, so ug refuses what it would.
    _check_writable(path)
    installed = _installed_id(settings, root)
    if installed is None:
        return False
    # An earlier build's install has its extension under its own plugin name.
    plugin = installed[1]
    config = _read_extension_config(root, plugin)
    if config is None:
        return False
    config["remove"] = True
    config["pluginRoot"] = str(root)
    atomic_write_json(_extension_dir(root, plugin) / "config.json", config)
    return True


def cleanup_leftover(root: Path | None = None, settings_path: Path | None = None) -> bool:
    """Delete the plugin directory once the extension has finished a deferred remove, which leaves
    it behind. Returns whether it was deleted."""
    root = root or plugin_root()
    if not _removal_requested(root):
        return False
    try:
        settings = read_settings(settings_path)
    except SettingsError:
        return False
    if _has_ug_keys(settings, root):
        return False
    _drop_settings(root, settings_path or copilot_settings_path(), settings)
    shutil.rmtree(root)
    return True


def status(root: Path | None = None, settings_path: Path | None = None) -> Status:
    root = root or plugin_root()
    try:
        settings = read_settings(settings_path)
    except SettingsError:
        return "invalid_settings"
    installed = _installed_id(settings, root)
    if installed is None:
        return "not_installed"
    marketplace, plugin = installed
    enabled = settings.get(_ENABLED_KEY)
    if isinstance(enabled, dict) and enabled.get(f"{plugin}@{marketplace}") is True:
        return "removal_pending" if _removal_requested(root) else "installed"
    return "disabled"
