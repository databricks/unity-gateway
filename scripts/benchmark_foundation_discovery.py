"""Offline configure/discovery benchmark; transport and auth are controlled, not live."""

import argparse
import contextlib
import json
import os
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
    parser.add_argument("--delay-ms", type=float, default=100)
    args = parser.parse_args()
    if args.runs < 1 or args.delay_ms < 0:
        parser.error("runs must be positive and delay-ms must be non-negative")
    sys.path.insert(0, str(args.source.resolve()))
    with tempfile.TemporaryDirectory(prefix="ug-foundation-benchmark-") as home:
        with patch.dict(
            os.environ,
            {
                "HOME": home,
                "USERPROFILE": home,
                "APPDATA": home,
                "LOCALAPPDATA": home,
                "PATH": os.defpath,
            },
            clear=True,
        ):
            from ucode import cli, databricks

            workspace = "https://benchmark.invalid"
            state = {"workspace": workspace, "profile": "benchmark"}
            payload = {
                "endpoints": [
                    {
                        "name": "shared-model",
                        "config": {
                            "served_entities": [
                                {
                                    "foundation_model": {
                                        "ai_gateway_v2_supported": True,
                                        "api_types": [
                                            "gemini/v1/generateContent",
                                            "openai/v1/responses",
                                        ],
                                    }
                                }
                            ]
                        },
                    }
                ]
            }
            calls = []

            def get(url, token, **kwargs):
                assert url == f"{workspace}/api/2.0/serving-endpoints:foundation-models", url
                assert token == "benchmark-token"
                calls.append(url)
                time.sleep(args.delay_ms / 1000)
                return payload, None

            with contextlib.ExitStack() as stack:
                for name, value in {
                    "load_state": state,
                    "save_state": None,
                    "ensure_databricks_auth": None,
                    "get_databricks_token": "benchmark-token",
                    "workspace_self_managed_agents": [],
                    "probe_unity_gateway_capabilities": databricks.GatewayProbe(True, "ok", True),
                    "discover_model_services": ({"sonnet": "uc-claude"}, [], [], [], None),
                    "print_success": None,
                    "print_warning": None,
                }.items():
                    stack.enter_context(patch.object(cli, name, return_value=value))
                stack.enter_context(
                    patch.object(cli, "spinner", side_effect=lambda *_: contextlib.nullcontext())
                )
                stack.enter_context(patch.object(databricks, "_http_get_json", side_effect=get))
                samples = []
                for run_index in range(args.runs + 1):
                    calls.clear()
                    started = time.perf_counter()
                    result = cli.configure_shared_state(
                        workspace, profile="benchmark", tools=["pi"]
                    )
                    elapsed_ms = (time.perf_counter() - started) * 1000
                    assert result["gemini_models"] == result["codex_models"] == ["shared-model"]
                    if run_index:
                        samples.append({"duration_ms": elapsed_ms, "requests": len(calls)})
            print(
                json.dumps(
                    {
                        "source": str(args.source.resolve()),
                        "python": sys.version,
                        "delay_ms": args.delay_ms,
                        "runs": args.runs,
                        "median_ms": statistics.median(row["duration_ms"] for row in samples),
                        "samples": samples,
                        "scope": "controlled configure_shared_state; excludes imports and live services",
                    },
                    indent=2,
                )
            )


if __name__ == "__main__":
    main()
