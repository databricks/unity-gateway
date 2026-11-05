"""Controlled proxy refresh benchmark; no network or credentials required.

Run with ``uv run scripts/benchmark_proxy_refresh.py``. ``--source`` selects a
baseline checkout using the same interpreter/dependencies. A token provider with
an explicit delay isolates serialized refresh cost; results are not live OAuth timings.
"""

import argparse
import json
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def measure(workers, delay):
    from ucode.gateway_proxy import TokenCache

    forces = []

    def provider(force):
        forces.append(force)
        if force:
            time.sleep(delay)
        return "benchmark-token"

    cache = TokenCache(provider)
    barrier = threading.Barrier(workers, timeout=10)

    def rejected_request():
        snapshot = (
            cache.token_snapshot() if hasattr(cache, "token_snapshot") else (cache.token, None)
        )
        barrier.wait()
        start = time.perf_counter()
        if snapshot[1] is None:
            cache.refresh()
        else:
            cache.refresh(rejected_generation=snapshot[1])
        return (time.perf_counter() - start) * 1000

    with ThreadPoolExecutor(max_workers=workers) as pool:
        values = list(pool.map(lambda _: rejected_request(), range(workers)))
    return {
        "forced_refreshes": sum(forces),
        "p50_ms": statistics.median(values),
        "last_ms": max(values),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--runs", type=int, default=12)
    parser.add_argument("--delay-ms", type=float, default=50)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be positive")
    sys.path.insert(0, args.source + "/src")
    report = {}
    for workers in (1, 8, 32):
        measure(workers, args.delay_ms / 1000)
        samples = [measure(workers, args.delay_ms / 1000) for _ in range(args.runs)]
        report[workers] = {
            "p50_ms": statistics.median(s["p50_ms"] for s in samples),
            "last_ms": statistics.median(s["last_ms"] for s in samples),
            "forced_refreshes": sorted({s["forced_refreshes"] for s in samples}),
            "samples": samples,
        }
    print(json.dumps({"simulated_refresh_ms": args.delay_ms, "results": report}, indent=2))


if __name__ == "__main__":
    main()
