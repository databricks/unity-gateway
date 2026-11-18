"""Cross-platform queries about the current process's rights and machine-wide folders."""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = [
    "is_admin",
    "user_profiles_dir",
    "windows_program_data_dir",
    "windows_program_files_dir",
    "windows_system32_dir",
]


def is_admin() -> bool:
    """Whether this process runs with administrator rights: elevated (or SYSTEM) on Windows, root
    elsewhere."""
    if sys.platform == "win32":
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    return os.geteuid() == 0


def user_profiles_dir() -> Path | None:
    """The folder that holds every user's home folder, or ``None`` where there is no standard one."""
    if sys.platform == "win32":
        return Path((os.environ.get("SystemDrive") or "C:") + "\\") / "Users"
    if sys.platform == "darwin":
        return Path("/Users")
    if sys.platform.startswith("linux"):
        return Path("/home")
    return None


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


def windows_system32_dir() -> Path:
    """The Windows system folder, for running system tools without searching ``PATH`` or the cwd."""
    return Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32"


def windows_admin_owned_path(path: Path) -> bool:
    """Whether only SYSTEM and Administrators can modify this Windows policy path.

    Fail closed for unreadable descriptors or unfamiliar ACLs. UG creates these ACLs with
    icacls; unlike POSIX mode bits, they remain meaningful for unelevated Windows readers.
    """
    import ctypes
    from ctypes import wintypes

    if sys.platform != "win32":
        return False
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_security = advapi.GetNamedSecurityInfoW
    get_security.argtypes = [
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    get_security.restype = wintypes.DWORD
    convert = advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW
    convert.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.c_void_p,
    ]
    convert.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    # SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION.
    if get_security(str(path), 1, 5, None, None, None, None, ctypes.byref(descriptor)):
        return False
    rendered = wintypes.LPWSTR()
    try:
        if not convert(descriptor, 1, 5, ctypes.byref(rendered), None):
            return False
        return _windows_policy_sddl_is_trusted(rendered.value or "")
    finally:
        if rendered:
            kernel.LocalFree(ctypes.cast(rendered, ctypes.c_void_p))
        kernel.LocalFree(descriptor)


def _windows_policy_sddl_is_trusted(sddl: str) -> bool:
    """Accept the administrator-owned, read-only-for-users ACLs UG publishes."""
    import re

    if not sddl.startswith(("O:BA", "O:SY")) or "D:" not in sddl:
        return False
    dacl = sddl.split("D:", 1)[1]
    entries = re.findall(r"\(([^()]*)\)", dacl)
    if not entries or re.sub(r"\([^()]*\)", "", dacl) not in ("", "P", "AI", "PAI"):
        return False
    for entry in entries:
        fields = entry.split(";")
        if len(fields) != 6 or fields[0] != "A" or fields[3] or fields[4]:
            return False
        if fields[5] in ("BA", "SY"):
            continue
        if fields[5] != "BU" or fields[2] not in ("FR", "FRFX", "0x1200a9"):
            return False
    return True
