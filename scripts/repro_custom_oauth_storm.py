#!/usr/bin/env python3
"""Reproduce slow-login retries and competing custom OAuth helpers, entirely offline.

Uses separate processes, ug's real token helper/cache, and the SDK's real consent
flow. Browser opening, callback listening, discovery, and token exchange are
mocked. A small driver enforces ug's generated Codex helper timeout and retries;
this does not run Codex or establish its actual retry policy.

Before the fix:
    uv run python scripts/repro_custom_oauth_storm.py --expect storm
After the fix, run the same scenarios:
    uv run python scripts/repro_custom_oauth_storm.py --expect fixed

No real credentials, browser tabs, network calls, or user configuration are used.
"""

from __future__ import annotations

import argparse
import multiprocessing
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from databricks.sdk import oauth

from ucode.agents import codex
from ucode.custom_oauth import CustomOAuthConfig, get_custom_client_token
from ucode.state import build_agent_state

WORKSPACE = "https://example.databricks.com"
CONFIG: CustomOAuthConfig = {
    "client_id": "repro-client",
    "redirect_url": "http://localhost:8020/repro",
    "scopes": ["offline_access", "model-serving"],
}


@dataclass
class Result:
    browsers: int
    succeeded: int
    failed: int
    timed_out: int

    @property
    def fixed(self) -> bool:
        return self.browsers == 1 and self.succeeded > 0 and not (self.failed or self.timed_out)


def _helper(cache_dir, login_seconds, ready, start, browsers, callback_port):
    """Run real ug/SDK code, replacing only external OAuth interactions."""
    callback_state = None

    def open_browser(_url):
        nonlocal callback_state
        callback_state = parse_qs(urlparse(_url).query)["state"][0]
        with browsers.get_lock():
            browsers.value += 1

    class CallbackServer:
        def __init__(self, _address, handler):
            # Preserve the SDK's browser-before-bind ordering and port contention.
            if not callback_port.acquire(block=False):
                raise OSError("simulated callback port already in use")
            self.feedback = handler.args[0]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            callback_port.release()

        def handle_request(self):
            time.sleep(login_seconds)
            self.feedback.append({"code": "fake-code", "state": callback_state})

    endpoints = oauth.OidcEndpoints(
        authorization_endpoint=f"{WORKSPACE}/oidc/v1/authorize",
        token_endpoint=f"{WORKSPACE}/oidc/v1/token",
    )
    token = oauth.Token(
        access_token="fake-access-token",
        refresh_token="fake-refresh-token",
        token_type="Bearer",
        expiry=datetime.now(UTC) + timedelta(hours=1),
    )
    with (
        patch.object(oauth.TokenCache, "BASE_PATH", cache_dir),
        patch.object(oauth, "get_workspace_endpoints", return_value=endpoints),
        patch.object(oauth, "retrieve_token", return_value=token),
        patch.object(oauth.webbrowser, "open_new", side_effect=open_browser),
        patch.object(oauth, "HTTPServer", CallbackServer),
        patch("ucode.custom_oauth.err_console", Mock()),
    ):
        ready.put(True)
        if not start.wait(30):
            raise SystemExit("driver did not start the helper")
        try:
            actual = get_custom_client_token(WORKSPACE, **CONFIG)
            if actual != token.access_token:
                raise SystemExit("unexpected synthetic token")
        except RuntimeError:
            raise SystemExit(1) from None


def run_helpers(cache_dir: Path, *, helpers: int, login_seconds: float, timeout: float) -> Result:
    """Start helpers together and enforce a common deadline after they are ready."""
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Queue()
    start = ctx.Event()
    browsers = ctx.Value("i", 0)
    callback_port = ctx.Semaphore(1)
    processes = [
        ctx.Process(
            target=_helper,
            args=(str(cache_dir), login_seconds, ready, start, browsers, callback_port),
        )
        for _ in range(helpers)
    ]
    timed_out = 0
    try:
        for process in processes:
            process.start()
        for _ in processes:
            ready.get(timeout=30)
        deadline = time.monotonic() + timeout
        start.set()
        for process in processes:
            process.join(timeout=max(0, deadline - time.monotonic()))
            if process.is_alive():
                timed_out += 1
                process.terminate()
                process.join(timeout=5)
        succeeded = sum(process.exitcode == 0 for process in processes)
        return Result(browsers.value, succeeded, helpers - succeeded - timed_out, timed_out)
    finally:
        for process in processes:
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
        ready.close()
        ready.join_thread()


def configured_timeout() -> float:
    with (
        patch.object(codex, "agent_version", return_value="test"),
        patch.object(codex, "ug_version", return_value="test"),
    ):
        doc = codex.render_overlay(WORKSPACE, custom_oauth=CONFIG)
        timeout_ms = doc["model_providers"]["ucode-databricks"]["auth"]["timeout_ms"]
        exported = build_agent_state({"workspace": WORKSPACE, "custom_oauth": CONFIG})
    assert timeout_ms == exported["codex"]["auth"]["timeout_ms"], "config emitters disagree"
    return timeout_ms / 1000


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--login-seconds", type=float, default=8)
    parser.add_argument("--helpers", type=int, default=5)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--expect", choices=("storm", "fixed"))
    args = parser.parse_args()
    if args.login_seconds <= 0 or args.helpers < 2 or args.attempts < 2:
        parser.error("use a positive login delay and at least two helpers/attempts")

    timeout = configured_timeout()
    print(f"Generated helper timeout: {timeout:g}s; simulated sign-in: {args.login_seconds:g}s")
    print("All external OAuth interactions mocked; the retry driver is simulated.", flush=True)
    with TemporaryDirectory(prefix="ug-oauth-repro-") as directory:
        root = Path(directory)
        slow = Result(0, 0, 0, 0)
        for attempt in range(1, args.attempts + 1):
            result = run_helpers(
                root / "slow", helpers=1, login_seconds=args.login_seconds, timeout=timeout
            )
            print(f"Slow login, attempt {attempt}: {result}", flush=True)
            for field in ("browsers", "succeeded", "failed", "timed_out"):
                setattr(slow, field, getattr(slow, field) + getattr(result, field))
            if result.succeeded:
                break

        # Give this case enough time even before the fix, isolating concurrency
        # from the short-timeout bug. All helpers share one cache and callback port.
        concurrent = run_helpers(
            root / "concurrent",
            helpers=args.helpers,
            login_seconds=args.login_seconds,
            timeout=max(timeout, args.login_seconds + 5),
        )
        print(f"Concurrent helpers: {concurrent}", flush=True)

    fixed = slow.fixed and concurrent.fixed and concurrent.succeeded == args.helpers
    storm = slow.browsers > 1 and slow.timed_out > 0 and concurrent.browsers > 1
    print("FIXED" if fixed else "STORM reproduced" if storm else "Unexpected outcome")
    if args.expect == "fixed":
        return 0 if fixed else 1
    if args.expect == "storm":
        return 0 if storm else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
