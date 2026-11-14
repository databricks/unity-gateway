"""Benchmark real Databricks CLI profile enumeration against a local HTTP responder.
Run ``uv run scripts/benchmark_profile_discovery.py``. Inert profiles and temporary
homes isolate credentials; this measures no live gateway or native agent launch."""

import argparse
import http.server
import json
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit


class Responder(http.server.BaseHTTPRequestHandler):
    paths: list[str] = []
    lock = threading.Lock()

    def do_GET(self):
        path = urlsplit(self.path).path
        with self.lock:
            self.paths.append(path)
        if path not in {"/.well-known/databricks-config", "/api/2.0/preview/scim/v2/Me"}:
            self.send_error(404)
            return
        time.sleep(self.delay_seconds)
        payload = b"{}"
        if path.endswith("/Me"):
            payload = b'{"id":"benchmark","userName":"benchmark@example.invalid"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass


def isolated_environment(directory: Path, config: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("DATABRICKS_", "UG_", "UCODE_", "BUNDLE_"))
        and key.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
    }
    for key, name in {
        "HOME": "home",
        "USERPROFILE": "home",
        "APPDATA": "appdata",
        "LOCALAPPDATA": "localappdata",
        "XDG_CONFIG_HOME": "config",
        "XDG_CACHE_HOME": "cache",
        "XDG_STATE_HOME": "state",
    }.items():
        path = directory / name
        path.mkdir(exist_ok=True)
        env[key] = str(path)
    env.update(DATABRICKS_CONFIG_FILE=str(config), NO_PROXY="127.0.0.1,localhost")
    return env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--profiles", type=int, default=4)
    parser.add_argument("--delay-ms", type=float, default=150)
    parser.add_argument("--databricks", default="databricks")
    args = parser.parse_args()
    if not 1 <= args.runs <= 1000 or not 1 <= args.profiles <= 32:
        parser.error("--runs must be 1–1000 and --profiles must be 1–32")
    if not 0 <= args.delay_ms <= 2000:
        parser.error("--delay-ms must be finite and between 0 and 2000")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from ucode.os_compatibility import subprocess_cross_os

    Responder.delay_seconds = args.delay_ms / 1000
    with tempfile.TemporaryDirectory(prefix="ug-profile-benchmark-") as temporary:
        directory = Path(temporary)
        config = directory / "databrickscfg"
        env = isolated_environment(directory, config)

        def command(argv):
            return subprocess_cross_os.run(
                [args.databricks, *argv],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                stdin=subprocess.DEVNULL,
                check=True,
            ).stdout

        version = command(["--version"]).strip()
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Responder)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host = f"http://127.0.0.1:{server.server_port}"

        def enumerate_profiles(skip, expected_names):
            first = len(Responder.paths)
            start = time.perf_counter()
            profiles = json.loads(
                command(
                    ["auth", "profiles", "--output", "json", *(["--skip-validate"] if skip else [])]
                )
            )["profiles"]
            sample = {
                "duration_ms": (time.perf_counter() - start) * 1000,
                "http_requests": len(Responder.paths) - first,
                "request_paths": Responder.paths[first:],
            }
            if {p["name"] for p in profiles} != expected_names:
                raise AssertionError("CLI returned unexpected profile names")
            if any(p["host"] != host for p in profiles):
                raise AssertionError("CLI did not preserve profile hosts")
            return sample, profiles

        try:
            config.write_text(
                "".join(
                    f"[bench-{i}]\nhost = {host}\nauth_type = pat\ntoken = inert-benchmark-token\n\n"
                    for i in range(args.profiles)
                ),
                encoding="utf-8",
            )
            expected = {f"bench-{i}" for i in range(args.profiles)}
            samples = {"validate": [], "skip_validate": []}
            for index in range(args.runs + 1):
                for skip in (False, True) if index % 2 == 0 else (True, False):
                    sample, profiles = enumerate_profiles(skip, expected)
                    checks = sample["request_paths"].count("/api/2.0/preview/scim/v2/Me")
                    if checks != (0 if skip else args.profiles):
                        raise AssertionError("unexpected number of profile validation requests")
                    if any(p["auth_type"] != "pat" for p in profiles):
                        raise AssertionError("CLI did not preserve explicit PAT auth type")
                    if not skip and not all(p.get("valid") for p in profiles):
                        raise AssertionError("local profile validation did not succeed")
                    if index:
                        samples["skip_validate" if skip else "validate"].append(sample)
            metadata = {}
            for label, auth, skip in (
                ("implicit_pat_validated", "token = inert-benchmark-token", False),
                ("implicit_pat_skip", "token = inert-benchmark-token", True),
                ("explicit_cli_skip", "auth_type = databricks-cli", True),
            ):
                config.write_text(
                    f"[metadata]\nhost = {host}\nworkspace_id = 123456\n{auth}\n",
                    encoding="utf-8",
                )
                _, profiles = enumerate_profiles(skip, {"metadata"})
                metadata[label] = {k: profiles[0].get(k) for k in ("auth_type", "workspace_id")}
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            if thread.is_alive():
                raise TimeoutError("local benchmark responder did not stop")
    result = {
        "cli_version": version,
        "profiles": args.profiles,
        "delay_ms": args.delay_ms,
        "warmups_each": 1,
        "runs_each": args.runs,
        "samples": samples,
        "medians_ms": {
            k: statistics.median(s["duration_ms"] for s in v) for k, v in samples.items()
        },
        "metadata_observed": metadata,
        "scope": "Local CLI component benchmark; no live gateway or native agent launch",
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
