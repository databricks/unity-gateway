"""Split the unit suite into time-balanced CI shards.

The units are each test slower than two seconds, on its own, and the rest of every test file,
split into interleaved chunks when slower than one chunk: chunk ``j`` of ``k`` keeps every
``k``-th of the file's remaining tests, starting at test ``j``. Units go slowest first to the
least-loaded shard. Each shard is given only its own files, so it imports and collects only what
it runs, and the shards together cover every unit test exactly once.

Weights come from tests/unit_test_times.json, measured on hosted runners for each OS: per file
``[seconds, tests]`` and per slow test seconds. Main records fresh measurements on every full run
(the `unit-test-times` artifact); commit them here when shards drift past their budget.

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
# Tests at least this slow are scheduled individually.
SLOW_SECONDS = 2.0
# The rest of a file slower than this splits into chunks.
CHUNK_SECONDS = 4.0
# Weight for a file the timings do not know yet.
UNKNOWN_FILE_SECONDS = 1.0

# ("test", nodeid) or ("chunk", file, chunk, chunks); chunks count from 1.
Unit = tuple


def parse(spec: str) -> tuple[int, int]:
    """``"3/8"`` -> ``(3, 8)``; shards count from 1."""
    index, total = (int(part) for part in spec.split("/"))
    if not 1 <= index <= total:
        raise ValueError(f"shard {spec!r} is out of range")
    return index, total


def test_files() -> list[str]:
    paths = (f"tests/{path.name}" for path in TESTS.glob("test_*.py"))
    return sorted(path for path in paths if path not in EXCLUDED)


def file_of(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


def plan(files: list[str], total: int, times: dict) -> list[list[Unit]]:
    """Units per shard: slowest first onto the least-loaded shard."""
    slow = {
        nodeid: seconds for nodeid, seconds in times["slow"].items() if file_of(nodeid) in files
    }
    units = [(("test", nodeid), seconds) for nodeid, seconds in slow.items()]
    for path in files:
        seconds = times["files"].get(path, [UNKNOWN_FILE_SECONDS])[0]
        rest = max(seconds - sum(s for nodeid, s in slow.items() if file_of(nodeid) == path), 0.0)
        chunks = max(1, math.ceil(rest / CHUNK_SECONDS))
        units += [(("chunk", path, chunk, chunks), rest / chunks) for chunk in range(1, chunks + 1)]
    shards: list[list[Unit]] = [[] for _ in range(total)]
    loads = [(0.0, shard) for shard in range(total)]
    for unit, seconds in sorted(units, key=lambda entry: (-entry[1], entry[0])):
        load, shard = heapq.heappop(loads)
        shards[shard].append(unit)
        heapq.heappush(loads, (load + seconds, shard))
    return shards


def runner_os() -> str:
    return "Windows" if sys.platform == "win32" else "Linux"


def load_times(system: str | None = None) -> dict:
    """``{"files": {path: [seconds, tests]}, "slow": {nodeid: seconds}}`` for this OS's runners."""
    return json.loads(TIMES.read_text(encoding="utf-8"))[system or runner_os()]


def units_for(spec: str) -> list[Unit]:
    index, total = parse(spec)
    return plan(test_files(), total, load_times())[index - 1]


def owned_tests(units: list[Unit], nodeids: list[str], slow: set[str]) -> list[bool]:
    """Whether each collected node id belongs to this shard's units."""
    tests = {unit[1] for unit in units if unit[0] == "test"}
    chunks = {(unit[1], unit[2]) for unit in units if unit[0] == "chunk"}
    count = {unit[1]: unit[3] for unit in units if unit[0] == "chunk"}
    position: dict[str, int] = {}
    owned = []
    for nodeid in nodeids:
        if nodeid in slow:
            owned.append(nodeid in tests)
            continue
        path = file_of(nodeid)
        index = position[path] = position.get(path, -1) + 1
        owned.append(path in count and (path, index % count[path] + 1) in chunks)
    return owned


if __name__ == "__main__":
    command, spec = sys.argv[1:]
    if command != "files":
        raise SystemExit(f"unknown command {command!r}")
    files = {unit[1] if unit[0] == "chunk" else file_of(unit[1]) for unit in units_for(spec)}
    print("\n".join(sorted(files)))
