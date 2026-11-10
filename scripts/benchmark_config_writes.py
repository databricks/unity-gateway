"""Measure repeated unchanged local config writes, not native-agent startup."""

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Checkout's src directory")
    parser.add_argument("--runs", type=int, default=15)
    parser.add_argument("--iterations", type=int, default=100)
    args = parser.parse_args()
    if args.runs < 1 or args.iterations < 1:
        parser.error("runs and iterations must be positive")
    sys.path.insert(0, str(args.source.resolve()))
    import tomlkit

    from ucode import config_io

    payload = {f"setting_{index}": "unchanged-value" for index in range(40)}
    document = tomlkit.document()
    document.update(payload)
    with tempfile.TemporaryDirectory(prefix="ug-config-benchmark-") as directory:
        root = Path(directory)

        def write_configs():
            config_io.write_text_file(root / "text", "unchanged\n" * 100)
            config_io.write_json_file(root / "json", payload)
            config_io.write_toml_file(root / "toml", document)
            config_io.write_dotenv(root / "dotenv", payload)

        write_configs()
        writes = 0
        original_write = Path.write_text

        def counted_write(path, *values, **options):
            nonlocal writes
            writes += 1
            return original_write(path, *values, **options)

        samples = []
        with patch.object(Path, "write_text", counted_write):
            for run_index in range(args.runs + 1):
                writes = 0
                started = time.perf_counter()
                for _ in range(args.iterations):
                    write_configs()
                per_iteration_ms = (time.perf_counter() - started) * 1000 / args.iterations
                if run_index:
                    samples.append({"duration_ms": per_iteration_ms, "writes": writes})
        print(
            json.dumps(
                {
                    "source": str(args.source.resolve()),
                    "python": sys.version,
                    "runs": args.runs,
                    "iterations": args.iterations,
                    "median_ms": statistics.median(row["duration_ms"] for row in samples),
                    "samples": samples,
                    "scope": "four unchanged local configs; excludes native agents and watchers",
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
