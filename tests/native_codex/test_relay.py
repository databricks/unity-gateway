"""Run the real Codex CLI through ug's real ChatGPT relay proxy to a fake AI Gateway.

Codex authenticates with a fake ChatGPT personal access token (`at-...`), so no ChatGPT
credential or network is needed. Codex validates such a token with a "whoami" call to
`$CODEX_AUTHAPI_BASE_URL/v1/user-auth-credential/whoami`; `CODEX_AUTHAPI_BASE_URL` is an
undocumented internal override (openai/codex `codex-rs/login/src/auth/personal_access_token.rs`,
used by Codex's own tests), and the stub below answers it. Codex's cloud-config fetch is
pointed at the same stub with `chatgpt_base_url`. Everything else is the shipped relayed
launch: `render_overlay(relayed=True)` plus `gateway_proxy.start_relay_proxy(codex=True)`.

This proves Codex's relayed requests arrive at the gateway with the ChatGPT identity intact and
ug's gateway headers added. It does not prove that the real gateway or ChatGPT accept the token.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests.test_gateway_proxy_integration import _FakeGateway
from ucode import gateway_proxy
from ucode.agents import codex
from ucode.codex_config import codex_config_args
from ucode.constants import LOOPBACK_HOST, MODEL_PROVIDER_SERVICE_HEADER

FAKE_TOKEN = "at-fake-relay-test"
ACCOUNT_ID = "acct-fake-relay-test"
PROVIDER = "main.ucode.fake_relay_mps"
ANSWER = "relay-ok-7c1"
RESPONSES_PATH = "/ai-gateway/codex/v1/responses"
MODELS_PATH = "/ai-gateway/codex/v1/models"


def _sse(*events: dict) -> list[bytes]:
    return [f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events]


ANSWER_STREAM = _sse(
    {"type": "response.created", "response": {"id": "resp-1"}},
    {
        "type": "response.output_item.done",
        "item": {
            "type": "message",
            "role": "assistant",
            "id": "msg-1",
            "content": [{"type": "output_text", "text": ANSWER}],
        },
    },
    {
        "type": "response.completed",
        "response": {
            "id": "resp-1",
            "usage": {
                "input_tokens": 0,
                "input_tokens_details": None,
                "output_tokens": 0,
                "output_tokens_details": None,
                "total_tokens": 0,
            },
        },
    },
)


@contextlib.contextmanager
def _chatgpt_stub():
    """Stands in for auth.openai.com (whoami) and chatgpt.com (cloud config bundle)."""
    seen: list[tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen.append((self.path, self.headers.get("Authorization", "")))
            if self.path == "/v1/user-auth-credential/whoami":
                body = {
                    "email": "relay-test@example.com",
                    "chatgpt_user_id": "user-fake-relay-test",
                    "chatgpt_account_id": ACCOUNT_ID,
                    "chatgpt_plan_type": "plus",
                    "chatgpt_account_is_fedramp": False,
                }
            elif self.path == "/backend-api/wham/config/bundle":
                body = {}
            else:
                self.send_error(404)
                return
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format, *args):  # noqa: A002
            pass

    server = ThreadingHTTPServer((LOOPBACK_HOST, 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://{LOOPBACK_HOST}:{server.server_address[1]}", seen
    finally:
        server.shutdown()
        server.server_close()


def _run_codex_through_relay(native_binary, tmp_path, gateway, token_provider):
    """Launch Codex the way a relayed `ug codex` does, against `gateway`; return (proc, stub_log)."""
    with _chatgpt_stub() as (stub_url, stub_seen):
        relay = gateway_proxy.start_relay_proxy(gateway.base_url, token_provider, 0, codex=True)
        threading.Thread(target=relay[0].serve_forever, daemon=True).start()
        try:
            overlay = codex.render_overlay(
                gateway.base_url, model="gpt-fake", provider=PROVIDER, relayed=True
            )
            port = relay[0].server_address[1]
            block = overlay["model_providers"][codex.CODEX_MODEL_PROVIDER_NAME]
            block["base_url"] = f"http://{LOOPBACK_HOST}:{port}/v1"
            home = tmp_path / "codex-home"
            home.mkdir()
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("CODEX_", "OPENAI_"))
            }
            env.update(
                CODEX_HOME=str(home),
                CODEX_ACCESS_TOKEN=FAKE_TOKEN,
                CODEX_AUTHAPI_BASE_URL=stub_url,
            )
            proc = subprocess.run(
                [
                    native_binary,
                    *codex_config_args(overlay),
                    "-c",
                    f'chatgpt_base_url="{stub_url}/backend-api"',
                    "exec",
                    "--json",
                    "--skip-git-repo-check",
                    "-C",
                    str(tmp_path),
                    "say the word",
                ],
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=60,
            )
        finally:
            relay[1].stop()
            relay[0].shutdown()
            relay[2].close()
        return proc, list(stub_seen)


def _agent_messages(stdout: str) -> list[str]:
    events = [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]
    return [
        event["item"]["text"]
        for event in events
        if event.get("type") == "item.completed" and event["item"].get("type") == "agent_message"
    ]


@pytest.fixture(autouse=True)
def _no_ambient_provider_headers(monkeypatch):
    """Keep a developer's smart-routing or request-tag env out of the rendered overlay."""
    for name in list(os.environ):
        if name.startswith(("UCODE_", "UG_")):
            monkeypatch.delenv(name, raising=False)


def test_codex_exec_through_relay_with_fake_access_token(native_binary, tmp_path):
    gateway = _FakeGateway(chunks=ANSWER_STREAM)
    gateway.start()
    try:
        proc, stub_seen = _run_codex_through_relay(
            native_binary, tmp_path, gateway, lambda force: "dbx-gateway-token"
        )
    finally:
        gateway.stop()

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _agent_messages(proc.stdout) == [ANSWER], proc.stdout

    # Codex validated the fake token with the stub, and nothing else was fetched.
    assert ("/v1/user-auth-credential/whoami", f"Bearer {FAKE_TOKEN}") in stub_seen

    # Besides the turn, Codex refreshes its model list (the fake's non-JSON reply is tolerated).
    paths = {r.path.split("?")[0] for r in gateway.requests}
    assert paths == {MODELS_PATH, RESPONSES_PATH}, paths
    (request,) = [r for r in gateway.requests if r.path == RESPONSES_PATH]
    assert request.method == "POST"
    # Codex's own ChatGPT identity passes through the relay untouched.
    assert request.header("authorization") == f"Bearer {FAKE_TOKEN}"
    assert request.header("chatgpt-account-id") == ACCOUNT_ID
    # ug adds the gateway credential and the provider the overlay selected.
    assert request.header(gateway_proxy.AI_GATEWAY_TOKEN_HEADER) == "Bearer dbx-gateway-token"
    assert request.header(MODEL_PROVIDER_SERVICE_HEADER) == PROVIDER
    assert request.header("accept-encoding") == "identity"
    assert json.loads(request.body)["model"] == "gpt-fake"
