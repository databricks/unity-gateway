"""Split the unit suite into time-balanced CI shards.

A shard runs a set of whole test files plus chunks of the files too slow for one shard: chunk
``j`` of ``k`` keeps every ``k``-th test of the file, starting at test ``j``. Each shard is given
only its own files, so it imports and collects only what it runs, and the shards together cover
every unit test file and every chunk exactly once. Weights come from tests/unit_test_times.json;
main records fresh measurements on every full run (the `unit-test-times` artifact). Commit them
here when shards drift past their budget.

``python -m tests.shard files <index>/<total>`` prints the shard's test files.
"""

from __future__ import annotations

import heapq
import json
import math
import sys
from pathlib import Path

TESTS = Path(__file__).parent
TIMES = TESTS / "unit_test_times.json"
# The live E2E suite runs in its own CI jobs.
EXCLUDED = {"tests/test_e2e.py"}
# Files slower than this split into chunks, so no file sets a shard's floor.
CHUNK_SECONDS = 4.0
# Weight for a file the timings do not know yet.
UNKNOWN_FILE_SECONDS = 1.0

Unit = tuple[str, int, int]  # (file, chunk, chunks); chunk counts from 1


def parse(spec: str) -> tuple[int, int]:
    """``"3/8"`` -> ``(3, 8)``; shards count from 1."""
    index, total = (int(part) for part in spec.split("/"))
    if not 1 <= index <= total:
        raise ValueError(f"shard {spec!r} is out of range")
    return index, total


def test_files() -> list[str]:
    paths = (f"tests/{path.name}" for path in TESTS.glob("test_*.py"))
    return sorted(path for path in paths if path not in EXCLUDED)


def plan(files: list[str], total: int, times: dict[str, list[float]]) -> list[list[Unit]]:
    """Units per shard: slowest first onto the least-loaded shard."""
    units = []
    for path in files:
        seconds = times.get(path, [UNKNOWN_FILE_SECONDS])[0]
        chunks = max(1, math.ceil(seconds / CHUNK_SECONDS))
        units += [((path, chunk, chunks), seconds / chunks) for chunk in range(1, chunks + 1)]
    shards: list[list[Unit]] = [[] for _ in range(total)]
    loads = [(0.0, shard) for shard in range(total)]
    for unit, seconds in sorted(units, key=lambda entry: (-entry[1], entry[0])):
        load, shard = heapq.heappop(loads)
        shards[shard].append(unit)
        heapq.heappush(loads, (load + seconds, shard))
    return shards


def load_times() -> dict[str, list[float]]:
    return json.loads(TIMES.read_text(encoding="utf-8"))


def units_for(spec: str) -> list[Unit]:
    index, total = parse(spec)
    return plan(test_files(), total, load_times())[index - 1]


if __name__ == "__main__":
    command, spec = sys.argv[1:]
    if command != "files":
        raise SystemExit(f"unknown command {command!r}")
    print("\n".join(sorted({path for path, _, _ in units_for(spec)})))
