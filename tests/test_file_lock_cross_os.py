"""Tests for cross-platform file locking."""

from __future__ import annotations

import errno
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import ucode.os_compatibility.file_lock_cross_os as file_lock_cross_os
from ucode.os_compatibility.file_lock_cross_os import _acquire_windows_exclusive_file_lock


def test_public_api_uses_windows_byte_range_lock(tmp_path, monkeypatch):
    locking = Mock()
    msvcrt = SimpleNamespace(locking=locking, LK_NBLCK=19, LK_UNLCK=20)

    with (tmp_path / "lock").open("a+b") as lock_file:
        fd = lock_file.fileno()
        monkeypatch.setattr(file_lock_cross_os.sys, "platform", "win32")
        monkeypatch.setitem(file_lock_cross_os.sys.modules, "msvcrt", msvcrt)

        file_lock_cross_os.acquire_exclusive_file_lock(lock_file)
        file_lock_cross_os.release_file_lock(lock_file)

    assert [entry.args for entry in locking.call_args_list] == [(fd, 19, 1), (fd, 20, 1)]


def test_windows_lock_retries_byte_range_contention(tmp_path, monkeypatch):
    attempts = []
    sleep = Mock()
    monkeypatch.setattr(file_lock_cross_os.time, "sleep", sleep)

    with (tmp_path / "lock").open("a+b") as lock_file:
        fd = lock_file.fileno()
        lock_file.seek(7)
        contention = OSError(errno.EACCES, "byte range is locked")
        contention.winerror = file_lock_cross_os._WINDOWS_LOCK_VIOLATION

        def locking(fd, mode, nbytes):
            attempts.append((fd, mode, nbytes, lock_file.tell()))
            if len(attempts) == 1:
                lock_file.seek(5)
                raise contention

        _acquire_windows_exclusive_file_lock(lock_file, locking=locking, lock_mode=19)

    assert attempts == [(fd, 19, 1, 0), (fd, 19, 1, 0)]
    sleep.assert_called_once_with(file_lock_cross_os._LOCK_POLL_SECONDS)


def test_windows_lock_propagates_permanent_errors(tmp_path, monkeypatch):
    error = OSError(errno.EBADF, "invalid file descriptor")
    locking = Mock(side_effect=error)
    sleep = Mock()
    monkeypatch.setattr(file_lock_cross_os.time, "sleep", sleep)

    with (tmp_path / "lock").open("a+b") as lock_file:
        fd = lock_file.fileno()
        with pytest.raises(OSError) as raised:
            _acquire_windows_exclusive_file_lock(lock_file, locking=locking, lock_mode=19)

    assert raised.value is error
    locking.assert_called_once_with(fd, 19, 1)
    sleep.assert_not_called()


def test_windows_lock_propagates_access_denied(tmp_path, monkeypatch):
    error = OSError(errno.EACCES, "access denied")
    error.winerror = 5
    locking = Mock(side_effect=error)
    sleep = Mock()
    monkeypatch.setattr(file_lock_cross_os.time, "sleep", sleep)

    with (tmp_path / "lock").open("a+b") as lock_file:
        fd = lock_file.fileno()
        with pytest.raises(OSError) as raised:
            _acquire_windows_exclusive_file_lock(lock_file, locking=locking, lock_mode=19)

    assert raised.value is error
    locking.assert_called_once_with(fd, 19, 1)
    sleep.assert_not_called()


def test_windows_nonblocking_lock_reports_contention(tmp_path, monkeypatch):
    locking = Mock(side_effect=OSError(errno.EACCES, "byte range is locked"))
    sleep = Mock()
    monkeypatch.setattr(file_lock_cross_os.time, "sleep", sleep)
    with (tmp_path / "lock").open("a+b") as lock_file:
        with pytest.raises(BlockingIOError):
            _acquire_windows_exclusive_file_lock(
                lock_file, locking=locking, lock_mode=19, blocking=False
            )
    assert locking.call_count == 1
    sleep.assert_not_called()
