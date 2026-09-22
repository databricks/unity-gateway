"""Harness-agnostic local accounting for completed coding-agent subagents."""

from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import tempfile
import time
from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from pathlib import Path

from ucode.config_io import APP_DIR

ENABLE_ENV_VAR = "ENABLE_SUBAGENT_USAGE_CSV"
RETENTION_SECONDS = 7 * 24 * 60 * 60
MAX_SESSION_CSV_BYTES = 10 * 1024 * 1024


def _nonnegative_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _text(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    return value if isinstance(value, str) else ""


@dataclass(frozen=True, slots=True)
class SubagentUsageRow:
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
    def build(
        payload: Mapping[str, object], *, now: float | None = None
    ) -> SubagentUsageRow | None:
        """Validate normalized harness output and build one typed CSV row."""
        session_id = payload.get("session_id")
        agent_id = payload.get("agent_id")
        if (
            not isinstance(session_id, str)
            or not session_id
            or not isinstance(agent_id, str)
            or not agent_id
        ):
            return None

        input_tokens = _nonnegative_int(payload.get("input_tokens"))
        cache_creation_input_tokens = _nonnegative_int(payload.get("cache_creation_input_tokens"))
        cache_read_input_tokens = _nonnegative_int(payload.get("cache_read_input_tokens"))
        output_tokens = _nonnegative_int(payload.get("output_tokens"))
        recorded_at_utc = _text(payload, "recorded_at_utc")
        if not recorded_at_utc:
            recorded_at = now if now is not None else time.time()
            recorded_at_utc = datetime.fromtimestamp(recorded_at, UTC).isoformat()

        return SubagentUsageRow(
            recorded_at_utc=recorded_at_utc,
            session_id=session_id,
            agent_id=agent_id,
            subagent_name=_text(payload, "subagent_name"),
            main_model=_text(payload, "main_model"),
            subagent_model=_text(payload, "subagent_model"),
            input_tokens=input_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
            output_tokens=output_tokens,
            total_tokens=(
                input_tokens + cache_creation_input_tokens + cache_read_input_tokens + output_tokens
            ),
            status=_text(payload, "status") or "ok",
        )

    def to_csv_dict(self) -> dict[str, object]:
        return asdict(self)


CSV_FIELDS = tuple(field.name for field in fields(SubagentUsageRow))


def enabled(env: MutableMapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return source.get(ENABLE_ENV_VAR) == "1"


def usage_directory() -> Path:
    return APP_DIR / "token-logs"


def session_csv_path(session_id: str) -> Path:
    if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", session_id):
        filename = session_id
    else:
        filename = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return usage_directory() / f"{filename}.csv"


def _ensure_usage_directory() -> Path:
    directory = usage_directory()
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
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _cleanup_stale_csvs(directory: Path, current: Path, now: float) -> None:
    for path in directory.glob("*.csv"):
        if path == current:
            continue
        try:
            if now - path.stat().st_mtime > RETENTION_SECONDS:
                path.unlink()
        except OSError:
            continue


def _existing_agent_ids(path: Path) -> set[str]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return {
                row["agent_id"]
                for row in csv.DictReader(handle)
                if isinstance(row.get("agent_id"), str)
            }
    except OSError:
        return set()


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


def _recorded_at_timestamp(row: SubagentUsageRow) -> float:
    try:
        return datetime.fromisoformat(row.recorded_at_utc.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


def write_subagent_usage(row: SubagentUsageRow, *, now: float | None = None) -> Path:
    """Append one typed row, deduplicating by agent id and cleaning up best effort."""
    path = session_csv_path(row.session_id)
    directory = _ensure_usage_directory()
    write_at = now if now is not None else _recorded_at_timestamp(row)
    with _directory_lock(directory):
        _cleanup_stale_csvs(directory, path, write_at)
        if path.exists() and row.agent_id in _existing_agent_ids(path):
            return path
        write_header = not path.exists() or path.stat().st_size == 0
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            if write_header:
                writer.writeheader()
            writer.writerow(row.to_csv_dict())
        if os.name != "nt":
            path.chmod(0o600)
        _compact(path)
    return path
