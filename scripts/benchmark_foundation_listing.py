"""Measure fallback listing reuse with controlled HTTP latency and no network calls.

Run with ``uv run --frozen scripts/benchmark_foundation_listing.py``. Use
``--source-directory /path/to/baseline/src`` to compare source trees with the same
interpreter and dependencies. This isolates discovery after a UC listing lacks
both Gemini and Codex models; it does not measure native agent readiness.
"""

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from unittest.mock import patch


def measure(delay: float) -> tuple[float, int]:
    from ucode import databricks

    requests = 0
    payload = {
        "endpoints": [
            {
                "name": "benchmark-model",
                "config": {
                    "served_entities": [
                        {
                            "foundation_model": {
                                "ai_gateway_v2_supported": True,
                                "api_types": ["gemini/v1/generateContent", "openai/v1/responses"],
                            }
                        }
                    ]
                },
            }
        ]
    }

    def get(url, token):
        nonlocal requests
        requests += 1
        time.sleep(delay)
        return payload, None

    with patch.object(databricks, "_http_get_json", get):
        start = time.perf_counter()
        if hasattr(databricks, "discover_gemini_and_codex_models"):
            result = databricks.discover_gemini_and_codex_models(
                "https://benchmark.invalid", "benchmark-token"
            )
        else:
            result = (
                databricks.discover_gemini_models("https://benchmark.invalid", "benchmark-token"),
                databricks.discover_codex_models("https://benchmark.invalid", "benchmark-token"),
            )
        elapsed = (time.perf_counter() - start) * 1000
    assert result == ((["benchmark-model"], None), (["benchmark-model"], None))
    return elapsed, requests


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-directory", type=Path, default=Path(__file__).resolve().parents[1] / "src"
    )
    parser.add_argument("--runs", type=int, default=11)
    parser.add_argument("--delay-ms", type=float, default=100)
    args = parser.parse_args()
    if args.runs < 1 or not 0 <= args.delay_ms <= 10_000:
        parser.error("--runs must be positive; --delay-ms must be between 0 and 10000")
    sys.path.insert(0, str(args.source_directory.resolve()))
    measure(args.delay_ms / 1000)
    samples = [measure(args.delay_ms / 1000) for _ in range(args.runs)]
    print(
        json.dumps(
            {
                "source_directory": str(args.source_directory.resolve()),
                "simulated_http_latency_ms": args.delay_ms,
                "median_ms": statistics.median(elapsed for elapsed, _ in samples),
                "requests_per_operation": sorted({requests for _, requests in samples}),
                "samples_ms": [elapsed for elapsed, _ in samples],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
