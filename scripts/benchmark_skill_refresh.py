"""Measure a skill refresh shared by several project installs, with controlled fetch cost.

Run with ``uv run scripts/benchmark_skill_refresh.py``; ``--source`` selects a
baseline checkout with the same dependencies. Metadata and bundle fetches are
substituted with delayed results; files and attribution use temporary directories.
No credentials or live APIs are used. Reports elapsed time and fetch counts.
"""

import argparse
import json
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--runs", type=int, default=8)
    parser.add_argument("--delay-ms", type=float, default=50)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be positive")
    sys.path.insert(0, args.source + "/src")
    from ucode import skills_download as sd
    from ucode import skills_state

    def measure(installs):
        counts = {"metadata": 0, "bundles": 0}
        counts_lock = threading.Lock()
        delay = args.delay_ms / 1000
        ref = sd.SkillRef(
            "benchmark", "skills", "shared", "shared", uc_update_time="2026-02-01T00:00:00Z"
        )

        def get(*_):
            with counts_lock:
                counts["metadata"] += 1
            time.sleep(delay)
            return ref

        def fetch(*_):
            with counts_lock:
                counts["bundles"] += 1
            time.sleep(delay)
            return {"SKILL.md": b"updated"}, None

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            records = []
            for index in range(installs):
                base = root / str(index)
                directory = base / ".claude/skills/shared"
                directory.mkdir(parents=True)
                (directory / "SKILL.md").write_bytes(b"old")
                records.append(
                    {
                        "base": str(base),
                        "fqn": ref.fqn,
                        "bundle_name": "shared",
                        "dirs": [str(directory)],
                        "uc_update_time": "2026-01-01T00:00:00Z",
                    }
                )
            with (
                patch.object(sd, "get_skill", get),
                patch.object(sd, "fetch_skill_bundle", fetch),
                patch.object(skills_state, "_skills_state_path", lambda: root / "manifest.json"),
                patch.object(sd.console, "print"),
            ):
                started = time.perf_counter()
                budget = 30 + installs * 4 * delay
                deadline = time.monotonic() + budget
                pairs = sd._refs_needing_refresh(
                    "https://benchmark.invalid", "dummy", records, deadline
                )
                updated = sd._update_stale_skills(
                    "https://benchmark.invalid", "dummy", pairs, deadline
                )
                elapsed = (time.perf_counter() - started) * 1000
                if updated != installs:
                    raise RuntimeError(
                        f"Only {updated}/{installs} installs refreshed within the {budget:g}s budget"
                    )
                recorded = len(skills_state.list_downloaded())
                if recorded != installs:
                    raise RuntimeError(f"Only {recorded}/{installs} installs recorded")
                for record in records:
                    path = Path(record["dirs"][0]) / "SKILL.md"
                    if path.read_bytes() != b"updated":
                        raise RuntimeError(f"Skill content was not refreshed: {path}")
        return {**counts, "elapsed_ms": elapsed}

    report = {}
    for installs in (1, 8, 32):
        measure(installs)
        samples = [measure(installs) for _ in range(args.runs)]
        report[installs] = {
            "median_ms": statistics.median(s["elapsed_ms"] for s in samples),
            "samples": samples,
        }
    print(json.dumps({"simulated_fetch_ms": args.delay_ms, "results": report}, indent=2))


if __name__ == "__main__":
    main()
