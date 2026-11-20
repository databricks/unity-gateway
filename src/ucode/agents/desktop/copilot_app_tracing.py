"""Turns the GitHub Copilot app's own trace export to the AI Gateway on and off.

The app's runtime exports its spans straight to the gateway once its environment has ``app_env``,
and gets each export's headers from ``ug otel-headers`` through ``telemetry.headersProvider`` in
Copilot's user settings. On macOS and Linux the variables reach the app through a block in the
login-shell profile that only the app's environment probe runs. On Windows, where the app runs no
probe, only the user's environment reaches it, and every program the user runs sees that, so ug
sets it there only when asked to.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ucode import config_io
from ucode.agents.copilot import APP_OTEL_ENV_NAMES, OTEL_TRACES_PATH
from ucode.agents.desktop import copilot_app_plugin, shell_profile_env
from ucode.managed_files import OS, current_os
from ucode.os_compatibility import windows_user_env
from ucode.ui import print_warning

# The runtime's default (29 minutes) is close to a Databricks OAuth token's one-hour life.
REFRESH_INTERVAL_MS = 1_500_000
# Earlier Copilot CLIs reject `telemetry.headersProvider` and then ignore the whole settings file
# they share with the app.
FIRST_HEADERS_PROVIDER_VERSION = (1, 0, 104)
WINDOWS_OPT_IN_FLAG = "--copilot-app-windows-tracing"

Status = Literal["on", "windows_opt_in", "not_set_up"]


def _record_path() -> Path:
    return config_io.APP_DIR / "copilot-app" / "tracing.json"


def _cache_dir() -> Path:
    home = Path.home()
    system = current_os()
    if system == OS.WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    if system == OS.MACOS:
        return home / "Library" / "Caches"
    return Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache")


def app_runtime_version() -> tuple[int, int, int] | None:
    """The newest Copilot runtime the app has downloaded, which reads Copilot's settings for it,
    or None before the app has downloaded one."""
    try:
        names = [entry.name for entry in (_cache_dir() / "github-copilot-sdk" / "cli").iterdir()]
    except OSError:
        return None
    versions = [
        (int(match[1]), int(match[2]), int(match[3]))
        for name in names
        if (match := re.search(r"(\d+)\.(\d+)\.(\d+)", name))
    ]
    return max(versions, default=None)


def app_env(workspace: str) -> dict[str, str]:
    values = (
        "true",
        workspace.rstrip("/") + OTEL_TRACES_PATH,
        # The gateway takes OTLP as protobuf; the runtime's default is JSON.
        "http/protobuf",
    )
    return dict(zip(APP_OTEL_ENV_NAMES, values, strict=True))


def headers_provider(ug_path: str, otel_headers_args: Sequence[str]) -> dict[str, Any]:
    return {
        "path": ug_path,
        "args": list(otel_headers_args),
        "refreshIntervalMs": REFRESH_INTERVAL_MS,
    }


@dataclass(frozen=True)
class _Record:
    profiles: tuple[Path, ...] = ()
    created: tuple[Path, ...] = ()
    windows_env: Mapping[str, str] | None = None

    @property
    def empty(self) -> bool:
        return not self.profiles and not self.windows_env


def _paths(value: object) -> tuple[Path, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(Path(item) for item in value if isinstance(item, str))


def _load() -> _Record:
    data = config_io.read_json_safe(_record_path())
    windows_env = data.get("windows_env")
    return _Record(
        profiles=_paths(data.get("profiles")),
        created=_paths(data.get("created")),
        windows_env=(
            {k: v for k, v in windows_env.items() if isinstance(v, str)}
            if isinstance(windows_env, dict)
            else None
        ),
    )


def _save(record: _Record) -> None:
    if record.empty:
        _record_path().unlink(missing_ok=True)
        return
    payload: dict[str, Any] = {
        "profiles": [str(path) for path in record.profiles],
        "created": [str(path) for path in record.created if path in record.profiles],
    }
    if record.windows_env:
        payload["windows_env"] = dict(record.windows_env)
    config_io.atomic_write_json(_record_path(), payload)


def _remove_profile(path: Path, created: Sequence[Path]) -> bool:
    changed = shell_profile_env.remove(path)
    # A profile ug created only for the block goes with it.
    if path in created and path.is_file() and not path.read_text(encoding="utf-8").strip():
        path.unlink()
    return changed


def _profiles_enable(env: Mapping[str, str], record: _Record) -> tuple[bool, _Record]:
    shells = shell_profile_env.login_shells()
    paths = list(
        dict.fromkeys(
            path for shell in shells if (path := shell_profile_env.profile_file(shell)) is not None
        )
    )
    if not paths:
        print_warning(
            "GitHub Copilot app tracing: the app doesn't read the profile of your login shell "
            f"({', '.join(shells) or 'none set'}), so its traces aren't exported. Supported: "
            f"{', '.join(shell_profile_env.SUPPORTED_SHELLS)}."
        )
        return _profiles_disable(record), _Record(windows_env=record.windows_env)
    created = list(dict.fromkeys([*record.created, *(path for path in paths if not path.exists())]))
    # Recorded before writing, so a write that fails partway still leaves revert every block.
    _save(
        _Record(
            tuple(dict.fromkeys([*record.profiles, *paths])), tuple(created), record.windows_env
        )
    )
    changed = False
    for path in paths:
        changed = shell_profile_env.write(path, env) or changed
    for stale in record.profiles:
        if stale not in paths:
            changed = _remove_profile(stale, created) or changed
    return changed, _Record(tuple(paths), tuple(created), record.windows_env)


def _profiles_disable(record: _Record) -> bool:
    changed = False
    for path in record.profiles:
        changed = _remove_profile(path, record.created) or changed
    return changed


def _windows_enable(
    env: Mapping[str, str], record: _Record, opted_in: bool
) -> tuple[bool, _Record]:
    if not opted_in:
        changed = _windows_disable(record)
        print_warning(
            "GitHub Copilot app tracing is off on Windows: it needs user-wide environment "
            "variables that every program you run sees. Turn it on with `ug configure --agents "
            f"copilot {WINDOWS_OPT_IN_FLAG}`."
        )
        return changed, _Record(record.profiles, record.created)
    previous = dict(record.windows_env or {})
    # ug's earlier values would otherwise count as the user's own and be kept.
    stale = {name: value for name, value in previous.items() if env.get(name) != value}
    changed = bool(stale) and windows_user_env.remove_values(stale)
    # Recorded first, for the same reason as the profiles.
    _save(_Record(record.profiles, record.created, {**previous, **env}))
    set_changed, kept = windows_user_env.set_values(env)
    if kept:
        print_warning(
            f"GitHub Copilot app tracing: left your own {', '.join(kept)} in place, so the app's "
            "traces may not reach the workspace."
        )
    written = {name: value for name, value in env.items() if name not in kept}
    return changed or set_changed, _Record(record.profiles, record.created, written)


def _windows_disable(record: _Record) -> bool:
    return bool(record.windows_env) and windows_user_env.remove_values(record.windows_env or {})


def enable(workspace: str, provider: Mapping[str, Any], *, windows_opt_in: bool) -> bool:
    """Export the app's traces to ``workspace``. Returns whether anything changed, which the app
    only picks up when it restarts. Raises ``copilot_app_plugin.SettingsError`` when Copilot's
    settings can't be rewritten."""
    existing = copilot_app_plugin.headers_provider()
    if existing is not None and not copilot_app_plugin.is_ug_headers_provider(existing):
        print_warning(
            "GitHub Copilot app tracing: your own telemetry.headersProvider in "
            f"{copilot_app_plugin.copilot_settings_path()} would send the app's traces without a "
            "Databricks token, so ug left tracing off. Remove it to turn tracing on."
        )
        return disable(keep_headers_provider=True)
    record = _load()
    env = app_env(workspace)
    if current_os() == OS.WINDOWS:
        changed, record = _windows_enable(env, record, windows_opt_in)
    else:
        changed, record = _profiles_enable(env, record)
    _save(record)
    if record.empty:
        return copilot_app_plugin.remove_headers_provider() or changed
    return copilot_app_plugin.set_headers_provider(provider) or changed


def disable(*, keep_headers_provider: bool = False) -> bool:
    """Stop exporting the app's traces: remove exactly what ``enable`` wrote. Returns whether
    anything was removed."""
    record = _load()
    changed = _profiles_disable(record)
    changed = _windows_disable(record) or changed
    _record_path().unlink(missing_ok=True)
    try:
        _record_path().parent.rmdir()
    except OSError:
        pass
    if not keep_headers_provider:
        changed = copilot_app_plugin.remove_headers_provider() or changed
    return changed


def status() -> Status:
    """Whether the app's traces are exported, as far as ug set it up."""
    try:
        provider = copilot_app_plugin.headers_provider()
    except copilot_app_plugin.SettingsError:
        provider = None
    record = _load()
    if not record.empty and copilot_app_plugin.is_ug_headers_provider(provider):
        return "on"
    if current_os() == OS.WINDOWS and not record.windows_env:
        return "windows_opt_in"
    return "not_set_up"
