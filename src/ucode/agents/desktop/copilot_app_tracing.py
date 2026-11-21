"""Removes ug's setup for the GitHub Copilot app runtime's own trace export: a guarded block in
login-shell profiles, Windows user environment variables, and ``telemetry.headersProvider`` in
Copilot's settings."""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path

from ucode import config_io
from ucode.agents.desktop import copilot_app_plugin

_BEGIN = "# >>> unity-gateway: GitHub Copilot app >>>"
_END = "# <<< unity-gateway: GitHub Copilot app <<<"
_FISH_FILE_NAME = "unity-gateway.fish"
_BLOCK = re.compile(rf"\n?{re.escape(_BEGIN)}\n.*?{re.escape(_END)}\n?", re.DOTALL)
_WINDOWS_ENV_KEY = "Environment"


def _record_path() -> Path:
    return config_io.APP_DIR / "copilot-app" / "tracing.json"


def _home() -> Path:
    return Path.home()


def _config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or _home() / ".config").expanduser()


def _profiles() -> list[Path]:
    home = _home()
    config_home = _config_home()
    return [
        home / ".zshenv",
        home / ".profile",
        home / ".bash_profile",
        home / ".bash_login",
        config_home / "fish" / "conf.d" / _FISH_FILE_NAME,
    ]


def _paths(value: object) -> list[Path]:
    return (
        [Path(item) for item in value if isinstance(item, str)] if isinstance(value, list) else []
    )


def _remove_block(path: Path, created: bool) -> bool:
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    if not _BLOCK.search(text):
        return False
    rest = _BLOCK.sub("\n", text).strip("\n")
    if not rest and (created or path.name == _FISH_FILE_NAME):
        path.unlink()
    else:
        config_io.write_text_file(path, rest + "\n" if rest else "")
    return True


def _broadcast_environment_change() -> None:
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    result = wintypes.DWORD()
    ctypes.windll.user32.SendMessageTimeoutW(
        0xFFFF, 0x001A, 0, _WINDOWS_ENV_KEY, 0x0002, 5000, ctypes.byref(result)
    )


def _remove_windows_env(values: Mapping[str, str]) -> bool:
    """Delete each user variable that still holds the value ug recorded writing."""
    if sys.platform != "win32" or not values:
        return False
    import winreg

    changed = False
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            _WINDOWS_ENV_KEY,
            0,
            winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE,
        ) as key:
            for name, value in values.items():
                try:
                    current, _ = winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    continue
                if current == value:
                    winreg.DeleteValue(key, name)
                    changed = True
    except FileNotFoundError:
        return False
    if changed:
        _broadcast_environment_change()
    return changed


def remove_legacy() -> bool:
    """Remove what an earlier build wrote for the app's tracing. Returns whether anything was
    removed; the running app keeps it until it restarts."""
    record = config_io.read_json_safe(_record_path())
    created = set(_paths(record.get("created")))
    changed = False
    for path in dict.fromkeys([*_paths(record.get("profiles")), *_profiles()]):
        changed = _remove_block(path, path in created) or changed
    windows_env = record.get("windows_env")
    if isinstance(windows_env, dict):
        values = {name: value for name, value in windows_env.items() if isinstance(value, str)}
        changed = _remove_windows_env(values) or changed
    changed = copilot_app_plugin.remove_headers_provider() or changed
    _record_path().unlink(missing_ok=True)
    try:
        _record_path().parent.rmdir()
    except OSError:
        pass
    return changed
