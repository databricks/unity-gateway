"""End-to-end tests for the relayed refresh proxy as a running server.

`test_gateway_proxy.py` covers the proxy's pieces with handler-level fakes
(`object.__new__(_ProxyHandler)`, `_FakeClient`, `_FakeResponse`) — the header
builder, the relay path, the token cache, and the retry-on-401 logic. What none
of those exercise is the whole thing wired together over real sockets: the
`ThreadingHTTPServer` bind, the `do_<METHOD>` dispatch, reading the body off a
real `rfile`, the pooled `httpx` client streaming to a real upstream and back,
and `_relay_response` writing to a real `wfile`.

These tests stand up a fake AI Gateway upstream, start the *real* relay or OTLP
proxy pointed at it, and drive it with a real HTTP client — so a regression
anywhere in that chain is caught.
Fully hermetic: no agent binary, no network, no workspace credentials.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from ucode import gateway_proxy


class _CapturedRequest:
    def __init__(self, method: str, path: str, headers: dict[str, str], body: bytes):
        self.method = method
        self.path = path
        # Keyed lowercase so assertions don't depend on header-case normalization.
        self.headers = headers
        self.body = body

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


class _FakeGateway:
    """Real HTTP server standing in for the workspace AI Gateway upstream.

    Records each forwarded request and returns a scripted response: a status code
    plus a list of body byte-chunks (flushed with an optional inter-chunk delay so
    a streaming relay is exercised, not just a single write)."""

    def __init__(
        self,
        status: int = 200,
        chunks: list[bytes] | None = None,
        sse_delay: float = 0.0,
        statuses: list[int] | None = None,
    ):
        self.requests: list[_CapturedRequest] = []
        self._status = status
        # Per-request statuses, in order; `status` applies once they run out.
        self._statuses = list(statuses or [])
        self._chunks = chunks if chunks is not None else [b'{"ok":true}']
        self._sse_delay = sse_delay
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> None:
        captured = self.requests
        status, chunks, sse_delay = self._status, self._chunks, self._sse_delay
        statuses = self._statuses

        class Handler(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                captured.append(
                    _CapturedRequest(
                        method=self.command,
                        path=self.path,
                        headers={k.lower(): v for k, v in self.headers.items()},
                        body=body,
                    )
                )
                self.send_response(statuses.pop(0) if statuses else status)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for chunk in chunks:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    if sse_delay:
                        time.sleep(sse_delay)

            def do_GET(self):  # noqa: N802
                self._serve()

            def do_POST(self):  # noqa: N802
                self._serve()

            def log_message(self, format, *args):  # noqa: A002
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None


@pytest.fixture
def make_gateway():
    """Factory that starts fake upstreams and tears them all down at test end."""
    gateways: list[_FakeGateway] = []

    def _make(**kwargs) -> _FakeGateway:
        gw = _FakeGateway(**kwargs)
        gw.start()
        gateways.append(gw)
        return gw

    yield _make
    for gw in gateways:
        gw.stop()


def _counting_token(value: str = "dbx-swap-token"):
    """A token_provider stand-in that records the force flag of each mint.

    Returns a plain (non-JWT) token, so `_jwt_exp` yields None and the cache falls
    back to the default TTL — the background refresher then never re-mints, keeping
    the mint count deterministic (one on init, one per forced retry-refresh)."""
    calls: list[bool] = []

    def provider(force_refresh: bool = False) -> str:
        calls.append(force_refresh)
        return value

    provider.calls = calls  # type: ignore[attr-defined]
    return provider


@contextlib.contextmanager
def _running_proxy(gateway: _FakeGateway, token_provider=None):
    """Start the real relay proxy pointed at `gateway`, yield its URL, then tear down."""
    server, cache, client = gateway_proxy.start_relay_proxy(
        gateway.base_url, token_provider or _counting_token(), 0
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        cache.stop()
        client.close()
        thread.join(timeout=2)


@contextlib.contextmanager
def _running_otel_proxy(gateway: _FakeGateway, token_provider=None):
    server, cache, client = gateway_proxy.start_otel_proxy(
        gateway.base_url, token_provider or _counting_token()
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        cache.stop()
        client.close()
        thread.join(timeout=2)


class TestRelayedProxyEndToEnd:
    def test_forwards_request_with_swap_header_and_passthrough(self, make_gateway):
        # The whole relayed data-plane over real sockets: the proxy injects a fresh
        # swap token, passes the caller's Anthropic OAuth + the MPS routing header
        # through untouched, forwards the body verbatim, and composes the upstream
        # path under /ai-gateway/anthropic/.
        gw = make_gateway()
        with _running_proxy(gw, _counting_token("swap-tok")) as proxy_url:
            resp = httpx.post(
                f"{proxy_url}/v1/messages",
                headers={
                    "Authorization": "Bearer anthropic-oauth",
                    "Databricks-Model-Provider-Service": "main.mcao.anthropic-mps",
                },
                content=b'{"model":"claude","stream":true}',
                timeout=10,
            )
        assert resp.status_code == 200
        assert resp.content == b'{"ok":true}'
        req = gw.requests[-1]
        assert req.method == "POST"
        assert req.path == "/ai-gateway/anthropic/v1/messages"
        assert req.header("X-Databricks-AI-Gateway-Token") == "Bearer swap-tok"
        assert req.header("Authorization") == "Bearer anthropic-oauth"
        assert req.header("Databricks-Model-Provider-Service") == "main.mcao.anthropic-mps"
        assert req.body == b'{"model":"claude","stream":true}'

    def test_streams_sse_chunks_back_in_order(self, make_gateway):
        # A relayed model turn streams SSE; the proxy must relay chunks through
        # rather than buffering the whole response. Assert the client receives the
        # full stream, in order, over a real socket.
        chunks = [b"event: a\ndata: 1\n\n", b"event: b\ndata: 2\n\n", b"event: c\ndata: 3\n\n"]
        gw = make_gateway(chunks=chunks, sse_delay=0.02)
        with _running_proxy(gw) as proxy_url:
            with httpx.Client(timeout=10) as client:
                with client.stream(
                    "POST",
                    f"{proxy_url}/v1/messages",
                    headers={"Authorization": "Bearer oauth"},
                    content=b"{}",
                ) as resp:
                    assert resp.status_code == 200
                    body = b"".join(resp.iter_raw())
        assert body == b"".join(chunks)

    def test_upstream_401_triggers_refresh_and_relays_over_socket(self, make_gateway):
        # A 401 may be a stale swap token, so the proxy force-refreshes and retries
        # once; when the retry still 401s it's genuinely the Anthropic layer and the
        # 401 is relayed verbatim (Claude Code then re-auths Anthropic). Exercised
        # here end-to-end over real sockets, not just the _handle fake path.
        token_fn = _counting_token("swap-tok")
        gw = make_gateway(status=401, chunks=[b'{"type":"error"}'])
        with _running_proxy(gw, token_fn) as proxy_url:
            resp = httpx.post(
                f"{proxy_url}/v1/messages",
                headers={"Authorization": "Bearer oauth"},
                content=b"{}",
                timeout=10,
            )
        assert resp.status_code == 401
        # Init mint isn't forced (force_refresh_near_expiry=False); the first 401 is
        # what forces a fresh mint before the single retry.
        assert token_fn.calls == [False, True]  # type: ignore[attr-defined]
        assert len(gw.requests) == 2  # original attempt + one retry


class TestOtelProxyEndToEnd:
    def test_otlp_body_forwards_with_token_in_authorization(self, make_gateway):
        # The OTLP seam: a protobuf body (no JSON model) forwards under
        # /ai-gateway/otel/ with the provider's token in Authorization and no relay
        # headers. The provider token stands in for a per-user custom-OAuth token.
        token = _counting_token("per-user-otel-token")
        gw = make_gateway()
        with _running_otel_proxy(gw, token) as proxy_url:
            resp = httpx.post(
                f"{proxy_url}/v1/traces",
                headers={"Content-Type": "application/x-protobuf"},
                content=b"\x0a\x02\x08\x01",  # arbitrary protobuf bytes, not JSON
                timeout=10,
            )
        assert resp.status_code == 200
        assert token.calls == [True]  # type: ignore[attr-defined]
        req = gw.requests[-1]
        assert req.path == "/ai-gateway/otel/v1/traces"
        assert req.header("Authorization") == "Bearer per-user-otel-token"
        assert req.header("X-Databricks-AI-Gateway-Token") is None
        assert req.body == b"\x0a\x02\x08\x01"


@contextlib.contextmanager
def _running_gateway_auth_proxy(
    gateway: _FakeGateway,
    token_provider=None,
    *,
    added_headers=None,
    on_refresh_failure=None,
    dropped_chat_completions_fields=(),
    **hooks,
):
    server, cache, client = gateway_proxy.start_gateway_auth_proxy(
        gateway.base_url,
        token_provider or _counting_token(),
        session_secret=_SESSION_SECRET,
        added_headers=added_headers or {},
        sign_in_hint="Run `databricks auth login`.",
        on_refresh_failure=on_refresh_failure or (lambda exc: None),
        dropped_chat_completions_fields=dropped_chat_completions_fields,
        **hooks,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        cache.stop()
        client.close()
        thread.join(timeout=2)


_SESSION_SECRET = "session-secret"


class TestGatewayAuthProxyEndToEnd:
    def test_swaps_session_secret_for_fresh_token_and_adds_headers(self, make_gateway):
        gw = make_gateway()
        with _running_gateway_auth_proxy(
            gw,
            _counting_token("dbx-token"),
            added_headers={"User-Agent": "ucode/1 copilot/2", "X-Tag": "t"},
        ) as proxy_url:
            resp = httpx.post(
                f"{proxy_url}/ai-gateway/mlflow/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {_SESSION_SECRET}",
                    "User-Agent": "OpenAI/JS 5.20.1",
                    "x-tag": "client-value",
                },
                content=b'{"model":"m"}',
                timeout=10,
            )
        assert resp.status_code == 200
        req = gw.requests[-1]
        assert req.path == "/ai-gateway/mlflow/v1/chat/completions"
        assert req.header("Authorization") == "Bearer dbx-token"
        assert req.header("User-Agent") == "ucode/1 copilot/2"
        assert req.header("X-Tag") == "t"
        assert req.body == b'{"model":"m"}'

    def test_drops_configured_fields_from_chat_completions_only(self, make_gateway):
        gw = make_gateway()
        body = b'{"model":"gpt-4o-mini","reasoning_effort":"medium","messages":[]}'
        with _running_gateway_auth_proxy(
            gw, dropped_chat_completions_fields=("reasoning_effort",)
        ) as proxy_url:
            for path in ("openai/v1/chat/completions", "openai/v1/responses"):
                httpx.post(
                    f"{proxy_url}/ai-gateway/{path}",
                    headers={"Authorization": f"Bearer {_SESSION_SECRET}"},
                    content=body,
                    timeout=10,
                )
        chat, responses = gw.requests
        assert json.loads(chat.body) == {"model": "gpt-4o-mini", "messages": []}
        assert responses.body == body

    def test_accepts_secret_in_x_api_key_and_does_not_forward_it(self, make_gateway):
        gw = make_gateway()
        with _running_gateway_auth_proxy(gw, _counting_token("dbx-token")) as proxy_url:
            resp = httpx.post(
                f"{proxy_url}/ai-gateway/anthropic/v1/messages",
                headers={"x-api-key": _SESSION_SECRET},
                content=b"{}",
                timeout=10,
            )
        assert resp.status_code == 200
        req = gw.requests[-1]
        assert req.path == "/ai-gateway/anthropic/v1/messages"
        assert req.header("x-api-key") is None
        assert req.header("Authorization") == "Bearer dbx-token"

    @pytest.mark.parametrize("credential", [None, "Bearer wrong", "Basic session-secret"])
    def test_rejects_missing_or_wrong_secret_without_forwarding(self, make_gateway, credential):
        gw = make_gateway()
        headers = {"Authorization": credential} if credential else {}
        with _running_gateway_auth_proxy(gw) as proxy_url:
            resp = httpx.post(
                f"{proxy_url}/ai-gateway/mlflow/v1/responses",
                headers=headers,
                content=b"{}",
                timeout=10,
            )
        assert resp.status_code == 401
        assert gw.requests == []

    def test_antigravity_session_rewrites_model_and_body_with_fresh_token(self, make_gateway):
        from ucode.agents import antigravity

        gw = make_gateway()
        catalog = {"gemini-3.8-flash": "system.ai.gemini-3-8-flash"}
        with _running_gateway_auth_proxy(
            gw,
            _counting_token("dbx-token"),
            added_headers=antigravity.proxy_headers("cat.sch.svc"),
            allowed_path_prefixes=(antigravity.GEMINI_GATEWAY_PATH,),
            path_rewriter=lambda path: antigravity.rewrite_model_path(path, catalog),
            body_rewriter=antigravity.rewrite_request_body,
        ) as proxy_url:
            resp = httpx.post(
                f"{proxy_url}/ai-gateway/gemini/v1beta/models/"
                "gemini-3.8-flash:streamGenerateContent?alt=sse",
                headers={"Authorization": f"Bearer {_SESSION_SECRET}"},
                json={
                    "generationConfig": {
                        "thinkingConfig": {"includeThoughts": True, "thinkingBudget": 0}
                    }
                },
                timeout=10,
            )
        assert resp.status_code == 200
        req = gw.requests[-1]
        assert req.path == (
            "/ai-gateway/gemini/v1beta/models/system.ai.gemini-3-8-flash"
            ":streamGenerateContent?alt=sse"
        )
        assert req.header("Authorization") == "Bearer dbx-token"
        assert req.header("Databricks-Model-Provider-Service") == "cat.sch.svc"
        assert json.loads(req.body) == {
            "generationConfig": {"thinkingConfig": {"thinkingBudget": 0}}
        }

    def test_antigravity_session_reaches_only_the_gemini_route(self, make_gateway):
        gw = make_gateway()
        with _running_gateway_auth_proxy(
            gw, allowed_path_prefixes=("/ai-gateway/gemini/",)
        ) as proxy_url:
            resp = httpx.post(
                f"{proxy_url}/ai-gateway/anthropic/v1/messages",
                headers={"Authorization": f"Bearer {_SESSION_SECRET}"},
                content=b"{}",
                timeout=10,
            )
        assert resp.status_code == 404
        assert gw.requests == []

    @pytest.mark.parametrize(
        "path",
        [
            "/api/2.0/clusters/list",
            "/ai-gateway/anthropic/../../api/2.0/clusters/list",
            "/ai-gateway/mlflow/v1/%2e%2e/%2e%2e/%2e%2e/api/2.0/clusters/list",
            "/ai-gateway/mlflow/v1/%252e%252e/%252e%252e/%252e%252e/api/2.0/clusters/list",
            "/ai-gateway/otel/v1/traces",
        ],
    )
    def test_refuses_paths_outside_model_routes(self, make_gateway, path):
        gw = make_gateway()
        with _running_gateway_auth_proxy(gw) as proxy_url:
            resp = httpx.post(
                f"{proxy_url}{path}",
                headers={"Authorization": f"Bearer {_SESSION_SECRET}"},
                content=b"{}",
                timeout=10,
            )
        assert resp.status_code == 404
        assert gw.requests == []

    def test_expired_token_is_refreshed_and_request_retried(self, make_gateway):
        # The session outlives the token: the gateway rejects the first one, the proxy
        # mints a new one and the same request succeeds without the agent restarting.
        tokens = iter(["expired-token", "fresh-token"])
        calls: list[bool] = []

        def provider(force_refresh: bool) -> str:
            calls.append(force_refresh)
            return next(tokens)

        gw = make_gateway(statuses=[401, 200])
        with _running_gateway_auth_proxy(gw, provider) as proxy_url:
            resp = httpx.post(
                f"{proxy_url}/ai-gateway/anthropic/v1/messages",
                headers={"Authorization": f"Bearer {_SESSION_SECRET}"},
                content=b"{}",
                timeout=10,
            )
        assert resp.status_code == 200
        assert calls == [True, True]
        assert [req.header("Authorization") for req in gw.requests] == [
            "Bearer expired-token",
            "Bearer fresh-token",
        ]

    @pytest.mark.parametrize(
        ("path", "expected_error"),
        [
            (
                "/ai-gateway/anthropic/v1/messages",
                {"type": "error", "error": {"type": "authentication_error"}},
            ),
            ("/ai-gateway/mlflow/v1/responses", {"error": {"type": "invalid_request_error"}}),
        ],
    )
    def test_failed_refresh_returns_actionable_401(
        self, make_gateway, capsys, path, expected_error
    ):
        minted: list[bool] = []

        def provider(force_refresh: bool) -> str:
            minted.append(force_refresh)
            if len(minted) > 1:
                raise RuntimeError("refresh token revoked")
            return "token"

        failures: list[str] = []
        gw = make_gateway(status=401)
        with _running_gateway_auth_proxy(
            gw, provider, on_refresh_failure=lambda exc: failures.append(str(exc))
        ) as proxy_url:
            resp = httpx.post(
                f"{proxy_url}{path}",
                headers={"Authorization": f"Bearer {_SESSION_SECRET}"},
                content=b"{}",
                timeout=10,
            )
        assert resp.status_code == 401
        body = resp.json()
        error = body["error"]
        assert error["type"] == expected_error["error"]["type"]
        assert body.get("type") == expected_error.get("type")
        assert "Databricks sign-in expired. Run `databricks auth login`." in error["message"]
        assert failures == ["refresh token revoked"]
        assert len(gw.requests) == 1
        assert capsys.readouterr().err == ""
