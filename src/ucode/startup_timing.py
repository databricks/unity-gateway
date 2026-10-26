"""Opt-in startup timings with no request or credential data."""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager

_STARTED_NS = time.perf_counter_ns()


def _enabled() -> bool:
    return os.environ.get("UG_STARTUP_TIMING") == "1"


def _emit(stage: str, outcome: str, started_ns: int) -> None:
    # Never let a closed diagnostics stream prevent a launch or hide its original failure.
    try:
        now_ns = time.perf_counter_ns()
        record = {
            "event": "ug_startup_timing",
            "stage": stage,
            "outcome": outcome,
            "pid": os.getpid(),
            "elapsed_ms": (now_ns - _STARTED_NS) / 1_000_000,
            "duration_ms": (now_ns - started_ns) / 1_000_000,
        }
        print(json.dumps(record, separators=(",", ":")), file=sys.stderr, flush=True)
    except Exception:
        pass


def startup_mark(stage: str) -> None:
    """Record a fixed, code-owned stage name; never pass user or server data here."""
    if _enabled():
        _emit(stage, "mark", time.perf_counter_ns())


@contextmanager
def startup_phase(stage: str) -> Iterator[None]:
    """Record phase boundaries, preserving all return values and exceptions."""
    if not _enabled():
        yield
        return
    started_ns = time.perf_counter_ns()
    _emit(stage, "started", started_ns)
    try:
        yield
    except BaseException:
        _emit(stage, "error", started_ns)
        raise
    else:
        _emit(stage, "ok", started_ns)
