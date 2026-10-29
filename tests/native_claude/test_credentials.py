"""Check that a real Claude ignores shell Anthropic credentials that ug's settings blank.

The blanks only work if Claude reads ``ANTHROPIC_API_KEY=""`` / ``ANTHROPIC_AUTH_TOKEN=""`` in
settings ``env`` as unset, with no empty header sent. Each case runs one ``claude -p`` turn against
a local fake API and records the credential headers on ``/v1/messages``.
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

from ucode.agents.claude import CLAUDE_BLANKED_CREDENTIAL_ENV_KEYS, _managed_settings_path

_SHELL_CREDENTIALS = {"ANTHROPIC_API_KEY": "shell-key", "ANTHROPIC_AUTH_TOKEN": "shell-token"}
_BLANKS = dict.fromkeys(CLAUDE_BLANKED_CREDENTIAL_ENV_KEYS, "")
_CREDENTIAL_HEADERS = ("authorization", "x-api-key")
_STREAM = "".join(
    f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
    for event in (
        {
            "type": "message_start",
            "message": {
                "id": "msg_fixture",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 1},
        },
        {"type": "message_stop"},
    )
).encode()


@pytest.fixture(autouse=True)
def _no_managed_settings():
    # Claude always loads the machine's managed settings, and ug's own would replace the fixture's.
    path = _managed_settings_path()
    if path is not None and path.exists():
        pytest.skip(f"{path} exists; run on a machine without Claude managed settings")


@contextmanager
def _fake_api() -> Iterator[tuple[str, list[dict[str, str]]]]:
    """Serve one-word turns; yields the base URL and each turn's credential headers."""
    turns: list[dict[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("content-length") or 0))
            is_turn = self.path.split("?")[0] == "/v1/messages"
            if is_turn:
                turns.append(
                    {
                        name.lower(): value
                        for name, value in self.headers.items()
                        if name.lower() in _CREDENTIAL_HEADERS
                    }
                )
            self.send_response(200)
            self.send_header("content-type", "text/event-stream" if is_turn else "application/json")
            self.end_headers()
            self.wfile.write(_STREAM if is_turn else b"{}")

        do_GET = do_POST

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", turns
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _turn_credentials(binary, tmp_path, settings: dict, env: dict[str, str]) -> dict[str, str]:
    """Run one ``claude -p`` turn with the shell credentials set; return the headers it sent."""
    with _fake_api() as (base_url, turns):
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
                "DISABLE_AUTOUPDATER": "1",
                **_SHELL_CREDENTIALS,
                **env,
            },
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )
    assert result.returncode == 0, result.stdout + result.stderr
    assert turns, "Claude sent no /v1/messages request"
    return turns[0]


@pytest.mark.parametrize(
    ("blanked", "expected"),
    [
        (True, {"authorization": "Bearer helper-token", "x-api-key": "helper-token"}),
        # Control: without the blanks the shell's credentials win, so the case above is meaningful.
        (False, {"authorization": "Bearer shell-token", "x-api-key": "shell-key"}),
    ],
    ids=["blanked", "control"],
)
def test_blanks_let_the_api_key_helper_win(native_binary, tmp_path, blanked, expected):
    settings = {"apiKeyHelper": "echo helper-token", **({"env": _BLANKS} if blanked else {})}

    assert _turn_credentials(native_binary, tmp_path, settings, {}) == expected


@pytest.mark.parametrize(
    ("blanked", "expected"),
    [
        (True, {"authorization": "Bearer oauth-token"}),
        (False, {"authorization": "Bearer shell-token", "x-api-key": "shell-key"}),
    ],
    ids=["blanked", "control"],
)
def test_relayed_blanks_let_the_subscription_token_win(native_binary, tmp_path, blanked, expected):
    """A relayed launch has no apiKeyHelper; Claude's own OAuth token must reach the relay."""
    settings = {"env": _BLANKS} if blanked else {}
    env = {"CLAUDE_CODE_OAUTH_TOKEN": "oauth-token"}

    assert _turn_credentials(native_binary, tmp_path, settings, env) == expected
