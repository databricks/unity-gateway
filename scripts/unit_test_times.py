"""Record per-file unit test times from a full CI run's shards, and prove they covered the suite.

usage: unit_test_times.py <directory of downloaded shard artifacts> <output json>

Each artifact holds one shard's JUnit XML and its report from tests/conftest.py: the units
(file, chunk, chunks) it owned and how many tests each of its files collected. For every OS,
each unit test file's chunks must appear exactly once across shards, and the tests run per file
must equal that file's collected count; anything else fails this job.
"""

import collections
import json
import sys
import xml.etree.ElementTree as ElementTree
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests import shard  # noqa: E402


def test_file(classname: str) -> str:
    parts = classname.split(".")
    return "/".join(parts[:2]) + ".py" if parts[0] == "tests" else classname


def main() -> None:
    artifacts, output = Path(sys.argv[1]), Path(sys.argv[2])
    files = shard.test_files()
    units = collections.defaultdict(collections.Counter)
    collected = collections.defaultdict(dict)
    ran = collections.defaultdict(collections.Counter)
    times = collections.defaultdict(lambda: collections.defaultdict(lambda: [0.0, 0]))
    for artifact in sorted(path for path in artifacts.iterdir() if path.is_dir()):
        system = artifact.name.split("-")[1]
        report = json.loads((artifact / "shard-report.json").read_text(encoding="utf-8"))
        units[system].update(tuple(unit) for unit in report["units"])
        collected[system].update(report["collected"])
        for case in ElementTree.parse(artifact / "junit.xml").iter("testcase"):
            path = test_file(case.get("classname", ""))
            ran[system][path] += 1
            times[system][path][0] += float(case.get("time", 0))
            times[system][path][1] += 1
    problems = []
    for system in sorted(units):
        chunks = {path: count for path, _, count in units[system]}
        if sorted(chunks) != files:
            problems.append(
                f"{system}: files missing from every shard: {sorted(set(files) - set(chunks))}"
            )
        for path, count in chunks.items():
            for chunk in range(1, count + 1):
                if units[system][(path, chunk, count)] != 1:
                    problems.append(
                        f"{system}: {path} chunk {chunk}/{count} ran {units[system][(path, chunk, count)]} times"
                    )
            if ran[system][path] != collected[system].get(path):
                problems.append(
                    f"{system}: {path} ran {ran[system][path]} of {collected[system].get(path)} tests"
                )
        print(f"{system}: {sum(ran[system].values())} tests across {len(chunks)} files")
    if problems:
        raise SystemExit("::error::Unit shards did not cover the suite:\n" + "\n".join(problems))
    rounded = {
        system: {
            path: [round(seconds, 2), count] for path, (seconds, count) in sorted(files.items())
        }
        for system, files in sorted(times.items())
    }
    output.write_text(json.dumps(rounded, indent=1, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
