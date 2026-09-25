"""Harness-agnostic local accounting for completed coding-agent subagents."""

from __future__ import annotations

import csv
import errno
import hashlib
import io
import os
import re
import tempfile
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, ClassVar

from ucode.config_io import APP_DIR

ENABLE_ENV_VAR = "ENABLE_SUBAGENT_USAGE_CSV"
RETENTION_SECONDS = 7 * 24 * 60 * 60
MAX_SESSION_CSV_BYTES = 10 * 1024 * 1024
LOCK_TIMEOUT_SECONDS = 2.0
LOCK_RETRY_SECONDS = 0.05


@dataclass(frozen=True, slots=True)
class SubagentUsageRow(ABC):
    token_log_subdirectory: ClassVar[str]

    recorded_at_utc: str
    session_id: str
    agent_id: str
    subagent_name: str
    main_model: str
    subagent_model: str
    input_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    output_tokens: int
    total_tokens: int
    status: str

    @staticmethod
    @abstractmethod
    def build(payload: Mapping[str, Any], *, now: float | None = None) -> SubagentUsageRow | None:
        """Build one harness-specific usage row from a hook payload."""
        raise NotImplementedError

    @classmethod
    def record(cls, payload: Mapping[str, Any], *, now: float | None = None) -> Path | None:
        """Build and persist one harness-specific usage row."""
        recorded_at = now if now is not None else time.time()
        row = cls.build(payload, now=recorded_at)
        if row is None:
            return None
        return write_subagent_usage(row, now=recorded_at)


CSV_FIELDS = tuple(field.name for field in fields(SubagentUsageRow))


def enabled(env: MutableMapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return source.get(ENABLE_ENV_VAR) == "1"


def usage_directory(token_log_subdirectory: str) -> Path:
    return APP_DIR / "token-logs" / token_log_subdirectory


def session_csv_path(session_id: str, token_log_subdirectory: str) -> Path:
    if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", session_id):
        filename = session_id
    else:
        filename = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return usage_directory(token_log_subdirectory) / f"{filename}.csv"


def _ensure_usage_directory(token_log_subdirectory: str) -> Path:
    directory = usage_directory(token_log_subdirectory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        directory.chmod(0o700)
    return directory


@contextmanager
def _directory_lock(directory: Path) -> Iterator[None]:
    lock_path = directory / ".write.lock"
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            if lock_path.stat().st_size == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            _acquire_lock_with_deadline(lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1))
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            _acquire_lock_with_deadline(lambda: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB))
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _acquire_lock_with_deadline(acquire: Callable[[], None]) -> None:
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:
        try:
            acquire()
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Timed out waiting {LOCK_TIMEOUT_SECONDS:g}s for the token-log lock"
                ) from exc
            time.sleep(min(LOCK_RETRY_SECONDS, remaining))


def _cleanup_stale_csvs(directory: Path, current: Path, now: float) -> None:
    for path in directory.glob("*.csv"):
        if path == current:
            continue
        try:
            if now - path.stat().st_mtime > RETENTION_SECONDS:
                path.unlink()
        except OSError:
            continue


def _write_rows_atomically(path: Path, rows: list[dict[str, str]]) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
    except OSError:
        Path(temporary).unlink(missing_ok=True)
        raise


def _compact(path: Path) -> None:
    if path.stat().st_size <= MAX_SESSION_CSV_BYTES:
        return
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS)
    writer.writeheader()
    size = len(buffer.getvalue().encode("utf-8"))
    kept: list[dict[str, str]] = []
    for row in reversed(rows):
        buffer.seek(0)
        buffer.truncate(0)
        writer.writerow(row)
        row_size = len(buffer.getvalue().encode("utf-8"))
        if size + row_size > MAX_SESSION_CSV_BYTES:
            break
        kept.append(row)
        size += row_size
    _write_rows_atomically(path, list(reversed(kept)))


def write_subagent_usage(row: SubagentUsageRow, *, now: float | None = None) -> Path:
    """Append one typed row and clean up old session files best effort."""
    path = session_csv_path(row.session_id, row.token_log_subdirectory)
    directory = _ensure_usage_directory(row.token_log_subdirectory)
    write_at = now if now is not None else time.time()
    with _directory_lock(directory):
        _cleanup_stale_csvs(directory, path, write_at)
        write_header = not path.exists() or path.stat().st_size == 0
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            if write_header:
                writer.writeheader()
            writer.writerow(asdict(row))
        if os.name != "nt":
            path.chmod(0o600)
        _compact(path)
    return path
