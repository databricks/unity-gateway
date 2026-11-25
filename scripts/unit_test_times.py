"""Record per-file unit test times from a full CI run's shards, and prove they covered the suite.

usage: unit_test_times.py <directory of downloaded shard artifacts> <output json>

Each artifact holds one shard's JUnit XML and its report from tests/conftest.py: the units it
owned (slow tests and file chunks) and how many tests each of its files collected. For every OS,
every unit must be owned exactly once, every test that ran must have run exactly once, the tests
run per file must equal that file's collected count, and both OSes' shards must be present;
anything else fails this job. The output has tests/unit_test_times.json's shape.
"""

import collections
import json
import sys
import xml.etree.ElementTree as ElementTree
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests import shard  # noqa: E402


def nodeid(case: ElementTree.Element) -> str:
    """JUnit classname ``tests.test_x.TestY`` and name -> ``tests/test_x.py::TestY::name``."""
    module, *classes = case.get("classname", "").split(".")[1:] or [""]
    return "::".join([f"tests/{module}.py", *classes, case.get("name", "")])


def main() -> None:
    artifacts, output = Path(sys.argv[1]), Path(sys.argv[2])
    files = shard.test_files()
    units = collections.defaultdict(collections.Counter)
    collected = collections.defaultdict(dict)
    ran = collections.defaultdict(collections.Counter)
    runs = collections.defaultdict(collections.Counter)
    by_file = collections.defaultdict(lambda: collections.defaultdict(lambda: [0.0, 0]))
    by_test = collections.defaultdict(collections.Counter)
    for artifact in sorted(path for path in artifacts.iterdir() if path.is_dir()):
        system = artifact.name.split("-")[2]  # unit-shard-<OS>-<n>
        report = json.loads((artifact / "shard-report.json").read_text(encoding="utf-8"))
        units[system].update(tuple(unit) for unit in report["units"])
        collected[system].update(report["collected"])
        for case in ElementTree.parse(artifact / "junit.xml").iter("testcase"):
            test = nodeid(case)
            path = shard.file_of(test)
            seconds = float(case.get("time", 0))
            ran[system][path] += 1
            runs[system][test] += 1
            by_file[system][path][0] += seconds
            by_file[system][path][1] += 1
            by_test[system][test] += seconds
    problems = []
    for system in ("Linux", "Windows"):
        if system not in units:
            problems.append(f"{system}: no unit shard artifacts")
    for system in sorted(units):
        chunks = {unit[1]: unit[3] for unit in units[system] if unit[0] == "chunk"}
        if sorted(chunks) != files:
            missing = sorted(set(files) - set(chunks))
            problems.append(f"{system}: files missing from every shard: {missing}")
        for path, count in chunks.items():
            for chunk in range(1, count + 1):
                seen = units[system][("chunk", path, chunk, count)]
                if seen != 1:
                    problems.append(f"{system}: {path} chunk {chunk}/{count} owned {seen} times")
        for unit, seen in units[system].items():
            if unit[0] == "test" and seen != 1:
                problems.append(f"{system}: slow test {unit[1]} owned {seen} times")
        for path in files:
            expected = collected[system].get(path)
            if ran[system][path] != expected:
                problems.append(f"{system}: {path} ran {ran[system][path]} of {expected} tests")
        for test, count in sorted(runs[system].items()):
            if count != 1:
                problems.append(f"{system}: {test} ran {count} times")
        print(f"{system}: {sum(ran[system].values())} tests across {len(chunks)} files")
    if problems:
        raise SystemExit("::error::Unit shards did not cover the suite:\n" + "\n".join(problems))
    times = {}
    for system in sorted(by_file):
        files_times = sorted(by_file[system].items())
        slow_tests = sorted(by_test[system].items())
        times[system] = {
            "files": {path: [round(seconds, 2), count] for path, (seconds, count) in files_times},
            "slow": {
                test: round(seconds, 2)
                for test, seconds in slow_tests
                if seconds >= shard.SLOW_SECONDS
            },
        }
    output.write_text(json.dumps(times, indent=1, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
