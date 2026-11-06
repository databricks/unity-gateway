"""Compare native catalog extraction in sequence and overlapping simulated preflight.

Uses the real selected Codex binary, isolated temporary homes, its bundled model
metadata, and its native catalog validation. No sign-in or inference is requested.
The preflight delay is simulated; results are component timings, not live startup p90.
"""

import argparse
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ucode.agents.codex_catalog import (
    prepare_codex_catalog,
    read_bundled_catalog_in_background,
)
from ucode.os_compatibility import subprocess_cross_os


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", default="codex")
    parser.add_argument("--model", help="Defaults to the first installed bundled model")
    parser.add_argument("--runs", type=int, default=11)
    parser.add_argument("--preflight-ms", type=float, default=300)
    args = parser.parse_args()
    if os.name != "posix":
        parser.error("overlapped extraction currently uses POSIX process groups")
    if args.runs < 1 or not math.isfinite(args.preflight_ms) or args.preflight_ms < 0:
        parser.error("runs must be positive and preflight-ms finite and nonnegative")
    binary = shutil.which(args.binary)
    if binary is None:
        parser.error("selected Codex binary is not installed")
    binary = str(Path(binary).absolute())
    samples = {"sequential": [], "overlapped": []}
    with tempfile.TemporaryDirectory(prefix="ug-catalog-benchmark-") as home:
        env = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "TMPDIR"}}
        env.update(HOME=home, CODEX_HOME=home, XDG_CONFIG_HOME=home, NO_COLOR="1")
        with patch.dict(os.environ, env, clear=True):

            def run(*flags):
                return subprocess_cross_os.run(
                    [binary, *flags],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=30,
                    cwd=home,
                ).stdout

            version = run("--version").strip()
            model = (
                args.model or json.loads(run("debug", "models", "--bundled"))["models"][0]["slug"]
            )
            for iteration in range(args.runs + 1):
                order = list(samples) if iteration % 2 else list(reversed(samples))
                catalogs = []
                for case in order:
                    start = time.perf_counter_ns()
                    if case == "overlapped":
                        with read_bundled_catalog_in_background(binary) as pending:
                            time.sleep(args.preflight_ms / 1000)
                            with redirect_stdout(sys.stderr):
                                result = prepare_codex_catalog(
                                    binary, [model], bundled_read=pending
                                )
                    else:
                        time.sleep(args.preflight_ms / 1000)
                        with redirect_stdout(sys.stderr):
                            result = prepare_codex_catalog(binary, [model])
                    elapsed = (time.perf_counter_ns() - start) / 1_000_000
                    catalogs.append(result)
                    if iteration:
                        samples[case].append(elapsed)
                assert catalogs[0] == catalogs[1], "native metadata changed between samples"
            assert run("--version").strip() == version, "Codex changed during the benchmark"
    print(
        json.dumps(
            {
                "binary": binary,
                "version": version,
                "model": model,
                "simulated_preflight_ms": args.preflight_ms,
                "runs": args.runs,
                "results": {
                    case: {"median_ms": statistics.median(values), "samples_ms": values}
                    for case, values in samples.items()
                },
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
