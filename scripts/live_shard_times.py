"""Record per-case live-shard times from a run's artifacts, and prove the shards covered the lane.

usage: live_shard_times.py <directory of downloaded shard artifacts> <output json>

Each artifact holds one shard's JUnit XML and the report tests/integration/conftest.py (or
tests/e2e_cuj/conftest.py) wrote: its units, the lane's collected files, the cases this shard
owned and the cases the marker filter dropped. For every lane, every unit must be owned exactly
once, the owned cases per file must add up to the lane's collected count, and the cases that ran
must equal the owned minus the marker-deselected count. The output has
tests/live_test_times.json's shape.
"""

import json
import sys
import xml.etree.ElementTree as ElementTree
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests import live_shard, shard  # noqa: E402


def nodeid(case: ElementTree.Element) -> str:
    """JUnit classname ``test_x.TestY`` (or ``tests.test_x.TestY``) and name -> ``test_x.py::TestY::name``."""
    parts = case.get("classname", "").split(".")
    parts = parts[1:] if parts and parts[0] == "tests" else parts
    module, *classes = parts or [""]
    return "::".join([f"{module}.py", *classes, case.get("name", "")])


def main() -> None:
    artifacts, output = Path(sys.argv[1]), Path(sys.argv[2])
    lanes: dict[tuple[str, str], list[tuple]] = {}
    problems = []
    # download-artifact makes one directory per artifact; dot-directories are harness noise.
    for artifact in sorted(
        path for path in artifacts.iterdir() if path.is_dir() and not path.name.startswith(".")
    ):
        reports = sorted(artifact.glob("*shard-report*.json"))
        junits = sorted(artifact.glob("*junit*.xml"))
        if not reports or not junits:
            problems.append(f"{artifact.name}: missing its shard report or JUnit XML")
            continue
        report = json.loads(reports[0].read_text(encoding="utf-8"))
        cases = list(ElementTree.parse(junits[0]).getroot().iter("testcase"))
        lanes.setdefault((report["lane"], report["os"]), []).append((report, cases))
    # The downloads fail open (an unmatched pattern downloads nothing), so the recorded shard
    # counts define the lanes this proof must see; a missing or renamed artifact fails it.
    expected = live_shard.expected_shards()
    for lane, (system, total) in sorted(expected.items()):
        shard_reports = lanes.get((lane, system))
        if not shard_reports:
            problems.append(f"{lane}/{system}: no shard artifacts downloaded")
            continue
        specs = sorted(shard.parse(report["shard"]) for report, _ in shard_reports)
        if specs != [(index, total) for index in range(1, total + 1)]:
            problems.append(
                f"{lane}/{system}: shard set is {[r['shard'] for r, _ in shard_reports]}"
            )
    times = {}
    for (lane, system), shard_reports in sorted(lanes.items()):
        label = f"{lane}/{system}"
        total = expected.get(lane, (system, len(shard_reports)))[1]
        specs = sorted(shard.parse(report["shard"]) for report, _ in shard_reports)
        if specs != [(index, total) for index in range(1, total + 1)]:
            problems.append(f"{label}: shard set is {[r['shard'] for r, _ in shard_reports]}")
        collected = [json.dumps(report["files"], sort_keys=True) for report, _ in shard_reports]
        if any(value != collected[0] for value in collected):
            problems.append(f"{label}: shards disagree on the lane's collected files")
        units: Counter = Counter()
        for report, _ in shard_reports:
            units.update(tuple(unit) for unit in report["units"])
        for unit, seen in units.items():
            if seen != 1:
                problems.append(f"{label}: unit {unit} owned {seen} times")
        by_file = {}
        by_case = Counter()
        by_case_seconds = {}
        for report, cases in sorted(
            shard_reports, key=lambda entry: shard.parse(entry[0]["shard"])
        ):
            expected = report["expected"]
            ran = Counter()
            for case in cases:
                test = nodeid(case)
                path = shard.file_of(test)
                ran[path] += 1
                by_case[test] += 1
                seconds = float(case.get("time", 0))
                by_case_seconds[test] = by_case_seconds.get(test, 0.0) + seconds
                entry = by_file.setdefault(path, [0.0, 0])
                entry[0] += seconds
                entry[1] += 1
            for path, owned in expected.items():
                marker_dropped = report["marker_deselected"].get(path, 0)
                if ran[path] != owned - marker_dropped:
                    problems.append(
                        f"{label} shard {report['shard']}: {path} ran {ran[path]} of "
                        f"{owned - marker_dropped} owned, marker-deselected cases"
                    )
            for path in ran:
                if path not in expected:
                    problems.append(f"{label} shard {report['shard']}: ran foreign file {path}")
        owned = Counter()
        for report, _ in shard_reports:
            owned.update(report["expected"])
        for path, count in sorted(Counter(shard_reports[0][0]["files"]).items()):
            if owned[path] != count:
                problems.append(f"{label}: {path} owned {owned[path]} of {count} collected cases")
        for test, seen in by_case.items():
            if seen != 1:
                problems.append(f"{label}: case {test} ran {seen} times")
        print(f"{label}: {len(shard_reports)} shards, {sum(v[1] for v in by_file.values())} cases")
        times[lane] = {
            system: {
                "shards": specs[0][1] if specs else 1,
                "files": {path: [round(sec, 2), n] for path, (sec, n) in sorted(by_file.items())},
                "slow": {
                    test: round(by_case_seconds[test], 2)
                    for test in sorted(by_case_seconds)
                    if by_case_seconds[test] >= shard.SLOW_SECONDS
                },
            }
        }
    if problems:
        raise SystemExit("::error::Live shards did not cover their lanes:\n" + "\n".join(problems))
    output.write_text(json.dumps(times, indent=1, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
