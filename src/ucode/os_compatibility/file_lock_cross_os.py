"""Cross-platform file locking."""

from __future__ import annotations

import errno
import sys
import time
from collections.abc import Callable
from typing import IO, Any

__all__ = ["acquire_exclusive_file_lock", "release_file_lock"]

_LOCK_POLL_SECONDS = 0.05
_WINDOWS_LOCK_VIOLATION = 33


def _acquire_windows_exclusive_file_lock(
    lock_file: IO[Any],
    *,
    locking: Callable[[int, int, int], None],
    lock_mode: int,
    timeout: float | None = None,
) -> None:
    _validate_timeout(timeout)
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        lock_file.seek(0)
        try:
            locking(lock_file.fileno(), lock_mode, 1)
            return
        except OSError as exc:
            # msvcrt reports a contended byte range as EACCES (WinError 33).
            winerror = getattr(exc, "winerror", None)
            if exc.errno != errno.EACCES or winerror not in (None, _WINDOWS_LOCK_VIOLATION):
                raise
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("timed out acquiring exclusive file lock") from exc
                time.sleep(min(_LOCK_POLL_SECONDS, remaining))
            else:
                time.sleep(_LOCK_POLL_SECONDS)


def _validate_timeout(timeout: float | None) -> None:
    if timeout is not None and timeout < 0:
        raise ValueError("timeout must be non-negative")


def _acquire_posix_exclusive_file_lock(lock_file: IO[Any], timeout: float | None) -> None:
    import fcntl

    if timeout is None:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        return

    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out acquiring exclusive file lock") from exc
            time.sleep(min(_LOCK_POLL_SECONDS, remaining))


def acquire_exclusive_file_lock(lock_file: IO[Any], *, timeout: float | None = None) -> None:
    _validate_timeout(timeout)
    if sys.platform != "win32":
        _acquire_posix_exclusive_file_lock(lock_file, timeout)
        return

    import msvcrt

    _acquire_windows_exclusive_file_lock(
        lock_file,
        locking=msvcrt.locking,
        lock_mode=msvcrt.LK_NBLCK,
        timeout=timeout,
    )


def release_file_lock(lock_file: IO[Any]) -> None:
    if sys.platform != "win32":
        import fcntl

        fcntl.flock(lock_file, fcntl.LOCK_UN)
        return

    import msvcrt

    lock_file.seek(0)
    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
