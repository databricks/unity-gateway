"""Cross-platform queries about the current process's rights and machine-wide folders."""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = [
    "is_admin",
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


def windows_owned_by_admin(path: Path) -> bool:
    """Whether Windows reports ``path``'s owner as Administrators, SYSTEM, or TrustedInstaller."""
    if sys.platform != "win32":
        raise NotImplementedError("file ownership is only checked on Windows")
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_info = advapi32.GetNamedSecurityInfoW
    get_info.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        *([ctypes.POINTER(ctypes.c_void_p)] * 5),
    ]
    get_info.restype = wintypes.DWORD
    sid_to_text = advapi32.ConvertSidToStringSidW
    sid_to_text.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    sid_to_text.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    se_file_object, owner_security_information = 1, 1
    owner, descriptor = ctypes.c_void_p(), ctypes.c_void_p()
    error = get_info(
        str(path),
        se_file_object,
        owner_security_information,
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
        if not sid_to_text(owner, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return text.value in _WINDOWS_ADMIN_OWNER_SIDS
        finally:
            kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        kernel32.LocalFree(descriptor)
