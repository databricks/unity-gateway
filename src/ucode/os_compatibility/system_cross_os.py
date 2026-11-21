"""Cross-platform queries about the current process's rights and machine-wide folders."""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = [
    "is_admin",
    "process_running",
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


def process_running(pid: int) -> bool:
    """Whether a process with ``pid`` exists. ``os.kill(pid, 0)`` would terminate it on Windows."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: it exists
        try:
            code = wintypes.DWORD()
            return (
                bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
            )
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


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
