"""Check that a real Claude stays on ANTHROPIC_BASE_URL when ug's settings zero a shell provider flag.

The fix only works if Claude reads ``CLAUDE_CODE_USE_BEDROCK="0"`` (etc.) in settings ``env`` as
off even when the shell sets it to ``1``. Each case runs one ``claude -p`` turn against a local fake
server that also stands in for the provider endpoint, and records which route the turn took.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests.native_claude.test_credentials import _STREAM
from ucode.agents.claude import CLAUDE_PROVIDER_ENV_KEYS, _managed_settings_path

_ZEROS = dict.fromkeys(CLAUDE_PROVIDER_ENV_KEYS, "0")
_PROVIDER_PREFIX = "/provider"
# Shell setup for each provider: its selection flag, its endpoint pointed at the fake server, and
# its auth skipped, so a control turn that leaves the gateway reaches no real cloud.
_PROVIDERS = {
    "bedrock": lambda base: {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "AWS_REGION": "us-west-2",
        "ANTHROPIC_BEDROCK_BASE_URL": base + _PROVIDER_PREFIX,
        "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1",
    },
    "vertex": lambda base: {
        "CLAUDE_CODE_USE_VERTEX": "1",
        "CLOUD_ML_REGION": "us-east5",
        "ANTHROPIC_VERTEX_PROJECT_ID": "fixture-project",
        "ANTHROPIC_VERTEX_BASE_URL": base + _PROVIDER_PREFIX,
        "CLAUDE_CODE_SKIP_VERTEX_AUTH": "1",
    },
}


@pytest.fixture(autouse=True)
def _no_managed_settings():
    # Claude always loads the machine's managed settings, and ug's own would replace the fixture's.
    path = _managed_settings_path()
    if path is not None and path.exists():
        pytest.skip(f"{path} exists; run on a machine without Claude managed settings")


@contextmanager
def _fake_server() -> Iterator[tuple[str, list[str]]]:
    """Answer gateway turns with a one-word reply; yields the base URL and every POST path."""
    paths: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("content-length") or 0))
            path = self.path.split("?")[0]
            paths.append(path)
            if path.startswith(_PROVIDER_PREFIX):
                # The provider route only needs to be seen, not answered.
                self.send_response(500)
                self.end_headers()
                return
            is_turn = path == "/v1/messages"
            self.send_response(200)
            self.send_header("content-type", "text/event-stream" if is_turn else "application/json")
            self.end_headers()
            self.wfile.write(_STREAM if is_turn else b"{}")

        do_GET = do_POST

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", paths
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _turn(binary, tmp_path, provider: str, settings: dict) -> tuple[int, list[str]]:
    """Run one ``claude -p`` turn with the provider flag set in the shell; return rc and paths."""
    with _fake_server() as (base_url, paths):
        result = subprocess.run(
            [
                binary,
                "-p",
                "hi",
                "--setting-sources",
                "user",
                "--settings",
                json.dumps(settings),
                "--model",
                "claude-sonnet-4-6",
            ],
            cwd=tmp_path,
            env={
                **{
                    key: os.environ[key]
                    for key in ("PATH", "SYSTEMROOT", "TEMP", "TMP")
                    if key in os.environ
                },
                "HOME": str(tmp_path),
                "CLAUDE_CONFIG_DIR": str(tmp_path / "config"),
                "ANTHROPIC_BASE_URL": base_url,
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "CLAUDE_CODE_MAX_RETRIES": "0",
                "DISABLE_AUTOUPDATER": "1",
                **_PROVIDERS[provider](base_url),
            },
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )
    return result.returncode, paths


@pytest.mark.parametrize("provider", sorted(_PROVIDERS))
def test_zeroed_flag_keeps_the_turn_on_the_gateway(native_binary, tmp_path, provider):
    settings = {"apiKeyHelper": "echo helper-token", "env": _ZEROS}

    returncode, paths = _turn(native_binary, tmp_path, provider, settings)

    assert returncode == 0
    assert "/v1/messages" in paths
    assert not any(path.startswith(_PROVIDER_PREFIX) for path in paths)


@pytest.mark.parametrize("provider", sorted(_PROVIDERS))
def test_control_shell_flag_sends_the_turn_to_the_provider(native_binary, tmp_path, provider):
    """Without the zeros the shell flag wins, so the case above is meaningful."""
    settings = {"apiKeyHelper": "echo helper-token"}

    _returncode, paths = _turn(native_binary, tmp_path, provider, settings)

    assert "/v1/messages" not in paths
    assert any(path.startswith(_PROVIDER_PREFIX) for path in paths)
