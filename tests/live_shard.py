"""Split one live CI lane into time-balanced shards, reusing tests/shard.py's balancer.

A lane is one slow live job that CI runs as several matrix legs (``UG_LIVE_SHARD=1/4`` ...), each
on its own runner so pytest stays serial inside a VM: configure and revert touch machine-wide
agent settings. Every case slower than two seconds — on live lanes, effectively all of them — is
scheduled on its own, slowest first onto the least-loaded shard, so the shards together run the
lane's cases exactly once.

Weights come from tests/live_test_times.json, recorded per lane and OS from real runs' JUnit
reports by scripts/live_shard_times.py; commit fresh recordings when shards drift past their
budget. The integration suite runs with only tests/integration on ``sys.path``, so its conftest
puts the repository root back before importing this module; the CUJ suite imports it directly.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from tests import shard as balance

TIMES = Path(__file__).with_name("live_test_times.json")


def lane_times(lane: str, system: str | None = None) -> dict:
    """``{"shards": n, "files": {path: [seconds, tests]}, "slow": {nodeid: seconds}}`` for a lane."""
    times = json.loads(TIMES.read_text(encoding="utf-8"))
    if lane not in times:
        raise ValueError(f"no recorded times for lane {lane!r} in {TIMES}; record them") from None
    try:
        return times[lane][system or balance.runner_os()]
    except KeyError:
        raise ValueError(
            f"no recorded times for lane {lane!r} on {system or balance.runner_os()}; "
            f"record them in {TIMES}"
        ) from None


def expected_shards() -> dict[str, tuple[str, int]]:
    """``{lane: (os, shards)}`` for every lane CI splits, from the committed weights."""
    return {
        lane: (system, times["shards"])
        for lane, oses in json.loads(TIMES.read_text(encoding="utf-8")).items()
        for system, times in oses.items()
        if times.get("shards", 1) > 1
    }


def units_for(
    lane: str, spec: str, files: list[str], system: str | None = None
) -> list[balance.Unit]:
    """This shard's units (cases and file chunks) among ``files``, from the committed weights."""
    index, total = balance.parse(spec)
    times = lane_times(lane, system)
    recorded = times.get("shards", 1)
    if total != recorded:
        raise ValueError(f"lane {lane!r} runs {recorded} shards, not {spec!r}")
    return balance.plan(files, total, times)[index - 1]


def plan_items(
    lane: str, spec: str, items: list, system: str | None = None
) -> tuple[list, list, dict]:
    """``(keep, drop, report)`` for one shard's already lane-filtered ``items``.

    ``report`` holds the static fields of the shard report the coverage proof reads; the caller
    adds ``marker_deselected`` once the marker filter has run.
    """
    files = sorted({balance.file_of(item.nodeid) for item in items})
    units = units_for(lane, spec, files, system)
    slow = set(lane_times(lane, system)["slow"])
    owned = balance.owned_tests(units, [item.nodeid for item in items], slow)
    keep = [item for item, mine in zip(items, owned, strict=True) if mine]
    drop = [item for item, mine in zip(items, owned, strict=True) if not mine]
    report = {
        "lane": lane,
        "os": system or balance.runner_os(),
        "shard": spec,
        "units": [list(unit) for unit in units],
        "files": dict(sorted(Counter(balance.file_of(i.nodeid) for i in items).items())),
        "expected": dict(sorted(Counter(balance.file_of(i.nodeid) for i in keep).items())),
    }
    return keep, drop, report


def write_report(path: str, report: dict, marker_deselected: dict) -> None:
    Path(path).write_text(
        json.dumps(
            {**report, "marker_deselected": dict(sorted(marker_deselected.items()))},
            indent=1,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def verify_shard(report_path: str, junit_path: str) -> None:
    """Fail unless one shard's JUnit ran exactly the cases its shard report owns.

    CI runs this in every launch-shard leg: the report exists only when the collection
    hook planned the lane, and its ``expected`` counts are this shard's slice, so a leg
    that ran another shard's case, lost one, or never sharded at all fails here. The
    offline contract tests prove the shards' plans partition the lane, so every case
    runs exactly once. Reruns write duplicate JUnit cases, so each case counts once.
    """
    import xml.etree.ElementTree as ElementTree

    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    ran: dict[str, set[str]] = {}
    for case in ElementTree.parse(junit_path).getroot().iter("testcase"):
        parts = case.get("classname", "").split(".")
        path = "/".join(parts[:2]) + ".py" if parts[:1] == ["tests"] else parts[0] + ".py"
        ran.setdefault(path, set()).add(case.get("name", ""))
    problems = []
    for path, owned in report["expected"].items():
        marker_dropped = report.get("marker_deselected", {}).get(path, 0)
        seen = ran.pop(path, set())
        if len(seen) != owned - marker_dropped:
            problems.append(f"{path}: ran {len(seen)} of {owned - marker_dropped} owned")
    for path in ran:
        problems.append(f"ran foreign file {path}")
    if problems:
        raise SystemExit("::error::Shard ran the wrong cases:\n" + "\n".join(problems))


if __name__ == "__main__":
    import sys

    if sys.argv[1:] and sys.argv[1] == "verify" and len(sys.argv) == 4:
        verify_shard(sys.argv[2], sys.argv[3])
    else:
        raise SystemExit("usage: python -m tests.live_shard verify <shard-report> <junit>")
