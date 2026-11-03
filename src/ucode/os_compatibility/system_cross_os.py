"""Cross-platform queries about the current process's rights and machine-wide folders."""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = [
    "is_admin",
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
