"""Tests for cross-platform process-rights and machine-wide folder queries."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import ucode.os_compatibility.system_cross_os as system_cross_os

# Captured before the autouse conftest guard replaces it with a stub that always says "not admin".
_REAL_IS_ADMIN = system_cross_os.is_admin


@pytest.mark.parametrize(("shell_result", "expected"), [(1, True), (0, False)])
def test_is_admin_on_windows_asks_the_shell(monkeypatch, shell_result, expected):
    shell32 = SimpleNamespace(IsUserAnAdmin=lambda: shell_result)
    monkeypatch.setattr(system_cross_os.sys, "platform", "win32")
    monkeypatch.setitem(
        system_cross_os.sys.modules,
        "ctypes",
        SimpleNamespace(windll=SimpleNamespace(shell32=shell32)),
    )

    assert _REAL_IS_ADMIN() is expected


@pytest.mark.parametrize(("euid", "expected"), [(0, True), (1000, False)])
def test_is_admin_elsewhere_means_root(monkeypatch, euid, expected):
    monkeypatch.setattr(system_cross_os.sys, "platform", "linux")
    monkeypatch.setattr(system_cross_os.os, "geteuid", lambda: euid, raising=False)

    assert _REAL_IS_ADMIN() is expected


@pytest.mark.parametrize(
    ("program_w6432", "program_files", "expected"),
    [
        (r"D:\Programs", r"D:\Programs (x86)", Path(r"D:\Programs")),
        (None, r"D:\Programs", Path(r"D:\Programs")),
        (None, None, Path(r"C:\Program Files")),
    ],
)
def test_windows_program_files_dir_prefers_the_64_bit_folder(
    monkeypatch, program_w6432, program_files, expected
):
    for name, value in (("ProgramW6432", program_w6432), ("ProgramFiles", program_files)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)

    assert system_cross_os.windows_program_files_dir() == expected


@pytest.mark.parametrize(
    ("program_data", "expected"),
    [(r"D:\Data", Path(r"D:\Data")), (None, Path(r"C:\ProgramData"))],
)
def test_windows_program_data_dir(monkeypatch, program_data, expected):
    if program_data is None:
        monkeypatch.delenv("ProgramData", raising=False)
    else:
        monkeypatch.setenv("ProgramData", program_data)

    assert system_cross_os.windows_program_data_dir() == expected


@pytest.mark.skipif(sys.platform != "win32", reason="asks Windows for the owner")
def test_windows_owned_by_admin_recognizes_the_windows_folder():
    assert system_cross_os.windows_owned_by_admin(Path(os.environ["SystemRoot"])) is True


@pytest.mark.skipif(sys.platform != "win32", reason="changes a real owner on Windows")
def test_windows_make_administrators_owner(tmp_path):
    # Only an elevated process (as on CI runners) may give a folder to Administrators.
    if not _REAL_IS_ADMIN():
        pytest.skip("needs an elevated process")
    folder = tmp_path / "owned"
    folder.mkdir()

    system_cross_os.windows_make_administrators_owner(folder)

    assert system_cross_os.windows_owned_by_admin(folder) is True


@pytest.mark.skipif(sys.platform == "win32", reason="covered by the real Windows checks")
def test_windows_ownership_is_windows_only(tmp_path):
    with pytest.raises(NotImplementedError):
        system_cross_os.windows_owned_by_admin(tmp_path)
    with pytest.raises(NotImplementedError):
        system_cross_os.windows_make_administrators_owner(tmp_path)
