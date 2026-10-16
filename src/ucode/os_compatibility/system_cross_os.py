"""Cross-platform queries about the current process's rights and machine-wide folders."""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = [
    "is_admin",
    "windows_make_administrators_owner",
    "windows_owned_by_admin",
    "windows_program_data_dir",
    "windows_program_files_dir",
]

# Administrators, SYSTEM, TrustedInstaller: owners a standard user can't be.
_WINDOWS_ADMIN_OWNER_SIDS = frozenset(
    {
        "S-1-5-32-544",
        "S-1-5-18",
        "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464",
    }
)


def is_admin() -> bool:
    """Whether this process runs with administrator rights: elevated (or SYSTEM) on Windows, root
    elsewhere."""
    if sys.platform == "win32":
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    return os.geteuid() == 0


def windows_program_files_dir() -> Path:
    """The 64-bit Program Files folder, where standard users can read but not write."""
    # ProgramW6432 is the 64-bit folder even from a 32-bit Python, whose ProgramFiles is (x86).
    return Path(
        os.environ.get("ProgramW6432") or os.environ.get("ProgramFiles") or r"C:\Program Files"
    )


def windows_program_data_dir() -> Path:
    """The ProgramData folder, where standard users can read and create entries but not change
    ones an administrator created."""
    return Path(os.environ.get("ProgramData") or r"C:\ProgramData")


_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 1
_ADMINISTRATORS_SID = "S-1-5-32-544"


def _windows_security_api():
    if sys.platform != "win32":
        raise NotImplementedError("file ownership is only managed on Windows")
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        *([ctypes.POINTER(ctypes.c_void_p)] * 5),
    ]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        *([ctypes.c_void_p] * 4),
    ]
    advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi32.ConvertStringSidToSidW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return ctypes, wintypes, advapi32, kernel32


def windows_owned_by_admin(path: Path) -> bool:
    """Whether Windows reports ``path``'s owner as Administrators, SYSTEM, or TrustedInstaller,
    the owners Codex itself expects for its ProgramData settings."""
    ctypes, wintypes, advapi32, kernel32 = _windows_security_api()
    owner, descriptor = ctypes.c_void_p(), ctypes.c_void_p()
    error = advapi32.GetNamedSecurityInfoW(
        str(path),
        _SE_FILE_OBJECT,
        _OWNER_SECURITY_INFORMATION,
        ctypes.byref(owner),
        None,
        None,
        None,
        ctypes.byref(descriptor),
    )
    if error:
        raise ctypes.WinError(error)
    try:
        text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(owner, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return text.value in _WINDOWS_ADMIN_OWNER_SIDS
        finally:
            kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        kernel32.LocalFree(descriptor)


def windows_make_administrators_owner(path: Path) -> None:
    """Hand ``path`` to the Administrators group unless an admin-only principal already owns it.

    An elevated process may create entries owned by its own account rather than the group (for
    example with UAC off, as on CI runners)."""
    if windows_owned_by_admin(path):
        return
    ctypes, _wintypes, advapi32, kernel32 = _windows_security_api()
    sid = ctypes.c_void_p()
    if not advapi32.ConvertStringSidToSidW(_ADMINISTRATORS_SID, ctypes.byref(sid)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        error = advapi32.SetNamedSecurityInfoW(
            str(path), _SE_FILE_OBJECT, _OWNER_SECURITY_INFORMATION, sid, None, None, None
        )
        if error:
            raise ctypes.WinError(error)
    finally:
        kernel32.LocalFree(sid)
