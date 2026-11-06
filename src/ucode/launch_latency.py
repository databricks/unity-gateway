"""Opt-in local measurements of UG entrypoint to native agent handoff.

Only explicitly selected launches produce records. Records contain bounded launch
metadata, never command arguments, prompts, workspace URLs, or credentials.
"""

from __future__ import annotations

import json
import os
import platform
import stat
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ucode.os_compatibility.file_lock_cross_os import (
    acquire_exclusive_file_lock,
    release_file_lock,
)

_PATH_ENV = "UG_LAUNCH_LATENCY_PATH"


@dataclass
class _Measurement:
    path: Path
    started_ns: int
    started_at: str
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    metadata: dict[str, str | bool] = field(default_factory=dict)
    selected: bool = False
    handed_off: bool = False


_current: _Measurement | None = None


def begin(started_ns: int) -> None:
    """Start before importing the CLI; unset/empty capture paths disable recording."""
    global _current
    _current = None
    path = os.environ.get(_PATH_ENV, "").strip()
    if path:
        try:
            _current = _Measurement(
                path=Path(path).expanduser(),
                started_ns=started_ns,
                started_at=datetime.now(UTC).isoformat(),
            )
        except Exception:  # noqa: BLE001 - invalid diagnostics paths cannot block the CLI
            return


def select(agent: str, *, skip_preflight: bool, refresh: bool = False) -> None:
    """Arm a launch, leaving helper commands and help requests unrecorded."""
    if _current is not None:
        _current.selected = True
        _current.metadata.update(agent=agent, skip_preflight=skip_preflight, refresh=refresh)


def update(
    *,
    managed: bool | None = None,
    auto_configure: bool | None = None,
    smart_routing: bool | None = None,
) -> None:
    """Attach resolved launch controls without copying state or environment values."""
    if _current is not None:
        for key, value in (
            ("managed", managed),
            ("auto_configure", auto_configure),
            ("smart_routing", smart_routing),
        ):
            if value is not None:
                _current.metadata[key] = value


def _write(measurement: _Measurement, outcome: str, ended_ns: int) -> None:
    # Capture must never change the launch's exit status or print on protocol streams.
    try:
        from ucode.telemetry import normalize_arch, normalize_os, ug_version

        record = {
            "schema_version": 1,
            "event_id": measurement.event_id,
            "started_at": measurement.started_at,
            "duration_ms": max(0, ended_ns - measurement.started_ns) / 1_000_000,
            "outcome": outcome,
            "ug_version": ug_version(),
            "os": normalize_os(sys.platform),
            "arch": normalize_arch(platform.machine()),
            **measurement.metadata,
        }
        measurement.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(measurement.path, flags, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as output:
            if not stat.S_ISREG(os.fstat(output.fileno()).st_mode):
                return
            acquire_exclusive_file_lock(output, timeout=0)
            try:
                output.write(json.dumps(record, separators=(",", ":")) + "\n")
                output.flush()
            finally:
                release_file_lock(output)
    except Exception:  # noqa: BLE001 - optional diagnostics must never block launching
        return


@contextmanager
def handoff() -> Iterator[None]:
    """Measure immediately before spawning/replacing UG, excluding session runtime.

    POSIX exec never returns, so write before execution. A failed spawn/exec
    appends a correction with the same event id; dashboard readers keep the last
    record. No lock remains held while executing or forking the native agent.
    """
    measurement = _current
    if measurement is None or not measurement.selected or measurement.handed_off:
        yield
        return
    ended_ns = time.perf_counter_ns()
    measurement.handed_off = True
    _write(measurement, "handoff", ended_ns)
    try:
        yield
    except BaseException:
        _write(measurement, "failed", time.perf_counter_ns())
        raise


def finish() -> None:
    """Record an armed launch that never reached handoff, then reset process state."""
    global _current
    measurement, _current = _current, None
    if measurement is not None and measurement.selected and not measurement.handed_off:
        _write(measurement, "failed", time.perf_counter_ns())
