"""Split the unit suite into time-balanced CI shards.

Every shard collects the whole suite and keeps its own slice, so the shards together run every
collected test exactly once, and a file slower than one shard's budget spreads across shards.
Weights come from tests/unit_test_times.json: each file's measured seconds over its test count.
Main records fresh measurements on every full run (the `unit-test-times` artifact); commit them
here when the shards drift past their budget.
"""

from __future__ import annotations

import json
from pathlib import Path

TIMES = Path(__file__).with_name("unit_test_times.json")
# Weight for tests in files the timings file does not know yet.
UNKNOWN_TEST_SECONDS = 0.05


def parse(spec: str) -> tuple[int, int]:
    """``"3/8"`` -> ``(3, 8)``; shards count from 1."""
    index, total = (int(part) for part in spec.split("/"))
    if not 1 <= index <= total:
        raise ValueError(f"shard {spec!r} is out of range")
    return index, total


def test_seconds(times: dict[str, list[float]]) -> dict[str, float]:
    """Average seconds per test for each file."""
    return {path: seconds / max(count, 1) for path, (seconds, count) in times.items()}


def assign(nodeids: list[str], total: int, per_test: dict[str, float]) -> list[int]:
    """Shard number (1-based) for each node id: slowest first onto the least-loaded shard."""
    weight = {
        nodeid: per_test.get(nodeid.split("::", 1)[0], UNKNOWN_TEST_SECONDS) for nodeid in nodeids
    }
    loads = [0.0] * total
    shard_of: dict[str, int] = {}
    for nodeid in sorted(nodeids, key=lambda nodeid: (-weight[nodeid], nodeid)):
        target = min(range(total), key=lambda shard: (loads[shard], shard))
        loads[target] += weight[nodeid]
        shard_of[nodeid] = target + 1
    return [shard_of[nodeid] for nodeid in nodeids]


def load_times() -> dict[str, list[float]]:
    return json.loads(TIMES.read_text(encoding="utf-8"))
