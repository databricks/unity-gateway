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
    blocking: bool = True,
) -> None:
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
            if not blocking:
                raise BlockingIOError(exc.errno, str(exc)) from exc
            time.sleep(_LOCK_POLL_SECONDS)


def acquire_exclusive_file_lock(lock_file: IO[Any], *, blocking: bool = True) -> None:
    if sys.platform != "win32":
        import fcntl

        fcntl.flock(lock_file, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return

    import msvcrt

    _acquire_windows_exclusive_file_lock(
        lock_file,
        locking=msvcrt.locking,
        lock_mode=msvcrt.LK_NBLCK,
        blocking=blocking,
    )


def release_file_lock(lock_file: IO[Any]) -> None:
    if sys.platform != "win32":
        import fcntl

        fcntl.flock(lock_file, fcntl.LOCK_UN)
        return

    import msvcrt

    lock_file.seek(0)
    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
