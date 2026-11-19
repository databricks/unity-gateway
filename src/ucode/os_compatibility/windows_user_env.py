"""Windows per-user environment variables (``HKCU\\Environment``), which apps started from
Explorer get once it is told they changed."""

from __future__ import annotations

import sys
from collections.abc import Mapping

_KEY = "Environment"
_HWND_BROADCAST = 0xFFFF
_WM_SETTINGCHANGE = 0x001A
_SMTO_ABORTIFHUNG = 0x0002


def _broadcast() -> None:
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    result = wintypes.DWORD()
    ctypes.windll.user32.SendMessageTimeoutW(
        _HWND_BROADCAST,
        _WM_SETTINGCHANGE,
        0,
        _KEY,
        _SMTO_ABORTIFHUNG,
        5000,
        ctypes.byref(result),
    )


def set_values(values: Mapping[str, str]) -> tuple[bool, list[str]]:
    """Set each variable unless the user already set it to something else.

    Returns whether anything changed and the names left alone."""
    if sys.platform != "win32":
        return False, []
    import winreg

    changed, kept = False, []
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, _KEY, 0, winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE
    ) as key:
        for name, value in values.items():
            try:
                current, _ = winreg.QueryValueEx(key, name)
            except FileNotFoundError:
                current = None
            if current == value:
                continue
            if current is not None:
                kept.append(name)
                continue
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
            changed = True
    if changed:
        _broadcast()
    return changed, kept


def remove_values(values: Mapping[str, str]) -> bool:
    """Delete each variable that still holds the given value. Returns whether any was deleted."""
    if sys.platform != "win32":
        return False
    import winreg

    changed = False
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _KEY, 0, winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE
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
        _broadcast()
    return changed
