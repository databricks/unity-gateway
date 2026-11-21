"""Record per-file unit test times from a full CI run's shard reports, and check coverage.

usage: unit_test_times.py <directory of downloaded shard artifacts> <output json>

Each shard artifact holds JUnit XML and the shard's coverage report from tests/conftest.py. The
shards of one OS must together run every collected test exactly once; any gap fails this job.
"""

import collections
import json
import sys
import xml.etree.ElementTree as ElementTree
from pathlib import Path


def test_file(classname: str) -> str:
    parts = classname.split(".")
    return "/".join(parts[:2]) + ".py" if parts[0] == "tests" else classname


def main() -> None:
    shards, output = Path(sys.argv[1]), Path(sys.argv[2])
    times = collections.defaultdict(lambda: [0.0, 0])
    by_os = collections.defaultdict(lambda: {"collected": set(), "selected": 0, "ran": 0})
    for artifact in sorted(path for path in shards.iterdir() if path.is_dir()):
        operating_system = artifact.name.split("-")[1]
        report = json.loads((artifact / "shard-report.json").read_text(encoding="utf-8"))
        cases = list(ElementTree.parse(artifact / "junit.xml").iter("testcase"))
        totals = by_os[operating_system]
        totals["collected"].add(report["collected"])
        totals["selected"] += report["selected"]
        totals["ran"] += len(cases)
        if operating_system == "Linux":
            for case in cases:
                entry = times[test_file(case.get("classname", ""))]
                entry[0] += float(case.get("time", 0))
                entry[1] += 1
    for operating_system, totals in by_os.items():
        collected = totals["collected"]
        print(
            f"{operating_system}: collected {collected}, selected {totals['selected']}, ran {totals['ran']}"
        )
        if (
            len(collected) != 1
            or totals["selected"] != totals["ran"]
            or totals["ran"] != collected.pop()
        ):
            raise SystemExit(
                f"::error::{operating_system} shards did not run every collected test once."
            )
    rounded = {path: [round(seconds, 2), count] for path, (seconds, count) in sorted(times.items())}
    output.write_text(json.dumps(rounded, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
