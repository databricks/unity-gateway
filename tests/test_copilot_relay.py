"""The GitHub Copilot seat relay, run as a real loopback server against fake upstreams."""

from __future__ import annotations

import contextlib
import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from ucode import copilot_relay

SECRET = "s3cret-path"
GITHUB_TOKEN = "gho_callertoken123"
DATABRICKS_TOKEN = "dbx-swap-token-456"
SESSION_TOKEN = "copilot-session-789"
MPS = "main.ai.copilot_seats"
COPILOT_HOST = "api.individual.githubcopilot.com"


class _Upstream:
    """Records each request and answers with a scripted status, headers and body."""

    def __init__(self, status: int = 200, body: bytes = b'{"ok":true}', headers=None):
        self.requests: list[dict] = []
        self.statuses: list[int] = []
        self._status = status
        self._body = body
        self._headers = headers or {"Content-Type": "application/json"}
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def _serve(self):
                length = int(self.headers.get("Content-Length", 0) or 0)
                upstream.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "names": [k.lower() for k in self.headers],
                        "body": self.rfile.read(length) if length else b"",
                    }
                )
                status = upstream.statuses.pop(0) if upstream.statuses else upstream._status
                self.send_response(status)
                for key, value in upstream._headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(upstream._body)))
                self.end_headers()
                self.wfile.write(upstream._body)

            def __getattr__(self, name):
                if name.startswith("do_"):
                    return self._serve
                raise AttributeError(name)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def gateway():
    upstream = _Upstream()
    yield upstream
    upstream.close()


@pytest.fixture
def github():
    upstream = _Upstream()
    yield upstream
    upstream.close()


@contextlib.contextmanager
def _relay(gateway, github, *, token_provider=None, copilot_host=None, **kwargs):
    direct: list[tuple[str, str]] = []
    server, cache, client = copilot_relay.start_copilot_relay_proxy(
        gateway.base_url,
        token_provider or (lambda force: DATABRICKS_TOKEN),
        session_secret=SECRET,
        provider_service=MPS,
        sign_in_hint="Run `databricks auth login`.",
        copilot_host=copilot_host or (lambda authorization: COPILOT_HOST),
        direct_base_url=lambda host: github.base_url if host == COPILOT_HOST else "https://wrong",
        on_direct_request=lambda method, path: direct.append((method, path)),
        **kwargs,
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10) as http:
            yield http, port, direct
    finally:
        server.shutdown()
        server.server_close()
        cache.stop()
        client.close()


def _auth(**extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {GITHUB_TOKEN}", **extra}


class TestGatewayRoutes:
    @pytest.mark.parametrize(
        ("method", "path", "gateway_path"),
        [
            ("POST", "/chat/completions", "/ai-gateway/openai/v1/chat/completions"),
            ("POST", "/responses", "/ai-gateway/openai/v1/responses"),
            ("POST", "/v1/messages", "/ai-gateway/anthropic/v1/messages"),
            ("GET", "/meta", "/ai-gateway/github-copilot/v1/meta"),
            ("GET", "/models", "/ai-gateway/v1/models"),
            ("POST", "/auto", "/ai-gateway/github-copilot/v1/auto"),
        ],
    )
    def test_swaps_credentials_onto_the_gateway_route(
        self, gateway, github, method, path, gateway_path
    ):
        with _relay(gateway, github) as (http, _port, direct):
            body = b'{"model":"m"}' if method == "POST" else None
            resp = http.request(
                method,
                f"/{SECRET}{path}",
                headers=_auth(**{"Copilot-Session-Token": SESSION_TOKEN}),
                content=body,
            )

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert github.requests == [] and direct == []
        (sent,) = gateway.requests
        assert (sent["method"], sent["path"]) == (method, gateway_path)
        assert sent["headers"]["authorization"] == f"Bearer {GITHUB_TOKEN}"
        assert sent["headers"]["x-databricks-ai-gateway-token"] == f"Bearer {DATABRICKS_TOKEN}"
        assert sent["headers"]["databricks-model-provider-service"] == MPS
        assert sent["headers"]["x-databricks-github-copilot-api-host"] == COPILOT_HOST
        assert sent["headers"]["copilot-session-token"] == SESSION_TOKEN
        assert sent["body"] == (body or b"")

    def test_keeps_the_query_and_replaces_client_databricks_headers(self, gateway, github):
        spoofed = {
            "X-Databricks-AI-Gateway-Token": "Bearer spoofed",
            "Databricks-Model-Provider-Service": "other.mps",
            "X-Databricks-GitHub-Copilot-Api-Host": "evil.example",
            "X-Databricks-Org-Id": "123",
        }
        with _relay(gateway, github) as (http, _port, _direct):
            http.get(f"/{SECRET}/models?filter=chat", headers=_auth(**spoofed))

        (sent,) = gateway.requests
        assert sent["path"] == "/ai-gateway/v1/models?filter=chat"
        assert sent["headers"]["x-databricks-ai-gateway-token"] == f"Bearer {DATABRICKS_TOKEN}"
        assert sent["headers"]["databricks-model-provider-service"] == MPS
        assert sent["headers"]["x-databricks-github-copilot-api-host"] == COPILOT_HOST
        assert "x-databricks-org-id" not in sent["headers"]
        assert sent["names"].count("x-databricks-ai-gateway-token") == 1

    def test_a_dead_databricks_login_answers_with_the_sign_in_hint(self, gateway, github):
        gateway._status = 401

        def token_provider(force: bool) -> str:
            if token_provider.calls:
                raise RuntimeError("refresh token expired")
            token_provider.calls += 1
            return DATABRICKS_TOKEN

        token_provider.calls = 0
        with _relay(gateway, github, token_provider=token_provider) as (http, _port, _direct):
            resp = http.post(f"/{SECRET}/chat/completions", headers=_auth(), content=b"{}")

        # Copilot hides 401 and 403 bodies.
        assert resp.status_code == 400
        assert "databricks auth login" in resp.json()["error"]["message"]

    def test_routes_can_send_a_call_direct(self, gateway, github):
        routes = {
            key: path for key, path in copilot_relay.GATEWAY_ROUTES.items() if key[1] != "/auto"
        }
        with _relay(gateway, github, gateway_routes=routes) as (http, _port, direct):
            http.post(f"/{SECRET}/auto", headers=_auth(), content=b"{}")

        assert gateway.requests == []
        assert [r["path"] for r in github.requests] == ["/auto"]
        assert direct == [("POST", "/auto")]


class TestDirectCalls:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/agents/swe/custom-agents/octo/repo"),
            ("GET", "/agents/swe/internal/memory/v0/items?limit=5"),
            ("POST", "/agents/sessions"),
            ("POST", "/models/session"),
            ("POST", "/models/session/intent"),
            ("GET", "/chat/completions"),
            ("POST", "/something/new"),
        ],
    )
    def test_go_to_the_copilot_host_without_databricks_credentials(
        self, gateway, github, method, path
    ):
        with _relay(gateway, github) as (http, _port, direct):
            resp = http.request(
                method,
                f"/{SECRET}{path}",
                headers=_auth(**{"X-Databricks-Org-Id": "123", "Copilot-Session-Token": "t"}),
                content=b"{}" if method == "POST" else None,
            )

        assert resp.status_code == 200
        assert gateway.requests == []
        (sent,) = github.requests
        assert (sent["method"], sent["path"]) == (method, path)
        assert sent["headers"]["authorization"] == f"Bearer {GITHUB_TOKEN}"
        assert sent["headers"]["copilot-session-token"] == "t"
        assert not [name for name in sent["names"] if "databricks" in name]
        assert DATABRICKS_TOKEN not in json.dumps(sent["headers"])
        assert direct == [(method, path.split("?", 1)[0])]

    def test_github_mcp_goes_direct_without_the_secret(self, gateway, github):
        with _relay(gateway, github) as (http, _port, direct):
            resp = http.post("/mcp/readonly", headers=_auth(), content=b"{}")

        assert resp.status_code == 200
        assert [r["path"] for r in github.requests] == ["/mcp/readonly"]
        assert gateway.requests == []
        assert direct == [("POST", "/mcp/readonly")]

    def test_redirects_are_relayed_not_followed(self, gateway, github):
        github._status = 302
        github._headers = {"Location": "https://elsewhere.example/steal"}
        with _relay(gateway, github) as (http, _port, _direct):
            resp = http.get(f"/{SECRET}/agents/swe/custom-agents/o/r", headers=_auth())

        assert resp.status_code == 302
        assert resp.headers["location"] == "https://elsewhere.example/steal"
        assert len(github.requests) == 1

    def test_an_unknown_copilot_host_is_refused(self, gateway, github):
        with _relay(gateway, github, copilot_host=lambda authorization: None) as (
            http,
            _port,
            _direct,
        ):
            resp = http.post(f"/{SECRET}/chat/completions", headers=_auth(), content=b"{}")

        assert resp.status_code == 400
        assert "Copilot API host" in resp.json()["error"]["message"]
        assert gateway.requests == [] and github.requests == []


class TestGate:
    @pytest.mark.parametrize(
        "path",
        [
            "/chat/completions",
            "/wrong-secret/chat/completions",
            f"/{SECRET}",
            f"/{SECRET}/",
            f"/{SECRET}//chat/completions",
            f"/{SECRET}/../{SECRET}/chat/completions",
            f"/{SECRET}/./chat/completions",
            f"/{SECRET}/%2e%2e/chat/completions",
            f"/{SECRET}%2fchat/completions",
            f"/{SECRET}/chat%5ccompletions",
            "/mcp/readonly/extra",
        ],
    )
    def test_refuses_paths_outside_the_secret(self, gateway, github, path):
        with _relay(gateway, github) as (_http, port, direct):
            # http.client sends the target as written; httpx would normalize dot segments.
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("POST", path, body=b"{}", headers=_auth())
            status = conn.getresponse().status
            conn.close()

        assert status == 404
        assert gateway.requests == [] and github.requests == [] and direct == []

    def test_refuses_an_absolute_form_target(self, gateway, github):
        with _relay(gateway, github) as (_http, port, _direct):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request(
                "POST",
                f"http://127.0.0.1:{port}/{SECRET}/chat/completions",
                body=b"{}",
                headers=_auth(),
            )
            status = conn.getresponse().status
            conn.close()

        assert status == 404
        assert gateway.requests == []

    @pytest.mark.parametrize(
        "headers",
        [
            {"Host": "localhost"},
            {"Host": "evil.example"},
            {"Origin": "https://evil.example"},
            {"Origin": "null"},
        ],
    )
    def test_refuses_other_hosts_and_browser_origins(self, gateway, github, headers):
        with _relay(gateway, github) as (http, _port, _direct):
            resp = http.post(f"/{SECRET}/chat/completions", headers=_auth(**headers), content=b"{}")

        assert resp.status_code == 403
        assert gateway.requests == []

    @pytest.mark.parametrize(
        "authorization",
        [None, "Bearer dapi123", "Basic gho_x", "gho_x", "Bearer sk-ant-oat01-x"],
    )
    def test_refuses_anything_but_one_github_bearer(self, gateway, github, authorization):
        headers = {"Authorization": authorization} if authorization else {}
        with _relay(gateway, github) as (http, _port, _direct):
            resp = http.post(f"/{SECRET}/chat/completions", headers=headers, content=b"{}")

        assert resp.status_code == 401
        assert gateway.requests == []

    def test_refuses_two_authorization_headers(self, gateway, github):
        with _relay(gateway, github) as (_http, port, _direct):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.putrequest("GET", f"/{SECRET}/meta")
            conn.putheader("Authorization", f"Bearer {GITHUB_TOKEN}")
            conn.putheader("Authorization", "Bearer gho_other")
            conn.endheaders()
            status = conn.getresponse().status
            conn.close()

        assert status == 401
        assert gateway.requests == []

    @pytest.mark.parametrize("token", ["gho_a", "ghu_a", "github_pat_a", "tid=a;exp=1;sku=x"])
    def test_accepts_each_github_credential_shape(self, gateway, github, token):
        with _relay(gateway, github) as (http, _port, _direct):
            resp = http.get(f"/{SECRET}/meta", headers={"Authorization": f"Bearer {token}"})

        assert resp.status_code == 200

    @pytest.mark.parametrize(
        ("method", "headers"),
        [("GET", {}), ("GET", {"Upgrade": "websocket", "Connection": "Upgrade"})],
    )
    def test_answers_websocket_responses_with_426(self, gateway, github, method, headers):
        with _relay(gateway, github) as (http, _port, _direct):
            resp = http.request(method, f"/{SECRET}/responses", headers=_auth(**headers))

        assert resp.status_code == 426
        assert gateway.requests == [] and github.requests == []

    def test_never_logs_credentials(self, gateway, github, tmp_path, monkeypatch):
        log = tmp_path / "relay.log"
        monkeypatch.setenv("UCODE_RELAYED_PROXY_DIAGNOSTICS", "1")
        monkeypatch.setenv("UCODE_RELAYED_PROXY_DIAGNOSTICS_FILE", str(log))
        server, cache, client = copilot_relay.start_copilot_relay_proxy(
            gateway.base_url,
            lambda force: DATABRICKS_TOKEN,
            session_secret=SECRET,
            provider_service=MPS,
            sign_in_hint="hint",
            copilot_host=lambda authorization: COPILOT_HOST,
            direct_base_url=lambda host: github.base_url,
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        headers = _auth(**{"Copilot-Session-Token": SESSION_TOKEN})
        try:
            httpx.post(f"{base}/{SECRET}/chat/completions", headers=headers, content=b"{}")
            httpx.get(f"{base}/{SECRET}/agents/swe/internal/memory/v0/x", headers=headers)
        finally:
            server.shutdown()
            server.server_close()
            cache.stop()
            client.close()

        text = log.read_text()
        assert "copilot_direct_request" in text
        for value in (GITHUB_TOKEN, DATABRICKS_TOKEN, SESSION_TOKEN, SECRET):
            assert value not in text


class TestCopilotApiHost:
    @pytest.mark.parametrize(
        ("url", "host"),
        [
            ("https://api.githubcopilot.com", "api.githubcopilot.com"),
            ("https://api.individual.githubcopilot.com", "api.individual.githubcopilot.com"),
            ("https://api.business.githubcopilot.com/", "api.business.githubcopilot.com"),
            ("https://API.Enterprise.githubcopilot.com:443", "api.enterprise.githubcopilot.com"),
            ("https://copilot-api.acme.ghe.com", "copilot-api.acme.ghe.com"),
        ],
    )
    def test_accepts_github_copilot_api_hosts(self, url, host):
        assert copilot_relay.copilot_api_host(url) == host

    @pytest.mark.parametrize(
        "url",
        [
            "http://api.individual.githubcopilot.com",
            "https://api.individual.githubcopilot.com:8443",
            "https://api.individual.githubcopilot.com/v1",
            "https://user@api.individual.githubcopilot.com",
            "https://api.individual.githubcopilot.com.evil.example",
            "https://evil.githubcopilot.com",
            "https://copilot-api.a.b.ghe.com",
            "https://copilot-api.acme.ghe.com.evil.example",
            "https://api.github.com",
            "not a url",
            None,
        ],
    )
    def test_refuses_anything_else(self, url):
        assert copilot_relay.copilot_api_host(url) is None


class TestCopilotHostResolver:
    def _client(self, calls: list, *, status=200, payload=None):
        def handle(request: httpx.Request) -> httpx.Response:
            calls.append((str(request.url), request.headers.get("authorization")))
            body = (
                payload
                if payload is not None
                else {"endpoints": {"api": f"https://{COPILOT_HOST}"}}
            )
            return httpx.Response(status, json=body)

        return httpx.Client(transport=httpx.MockTransport(handle))

    def test_asks_github_once_per_credential(self, tmp_path):
        calls: list = []
        resolver = copilot_relay.CopilotHostResolver(
            client=self._client(calls), cache_file=tmp_path / "missing.json"
        )

        assert resolver(f"Bearer {GITHUB_TOKEN}") == COPILOT_HOST
        assert resolver(f"Bearer {GITHUB_TOKEN}") == COPILOT_HOST
        assert calls == [("https://api.github.com/copilot_internal/user", f"Bearer {GITHUB_TOKEN}")]

    def test_falls_back_to_the_newest_cache_entry(self, tmp_path):
        cache = tmp_path / "copilot-user-cache.json"
        cache.write_text(
            "// written by Copilot\n"
            + json.dumps(
                {
                    "a": {
                        "retrievedAt": "2026-11-01T10:00:00Z",
                        "endpoints": {"api": "https://api.business.githubcopilot.com"},
                    },
                    "b": {
                        "retrievedAt": "2026-11-17T10:00:00Z",
                        "endpoints": {"api": "https://copilot-api.acme.ghe.com"},
                    },
                    "c": {
                        "retrievedAt": "2026-11-18T10:00:00Z",
                        "endpoints": {"api": "https://evil.example"},
                    },
                }
            )
        )
        calls: list = []
        resolver = copilot_relay.CopilotHostResolver(
            client=self._client(calls, status=401), cache_file=cache
        )

        assert resolver(f"Bearer {GITHUB_TOKEN}") == "copilot-api.acme.ghe.com"
        assert len(calls) == 1

    def test_vscode_short_lived_tokens_skip_github(self, tmp_path):
        calls: list = []
        resolver = copilot_relay.CopilotHostResolver(
            client=self._client(calls), cache_file=tmp_path / "missing.json"
        )

        assert resolver("Bearer tid=abc;exp=1") is None
        assert calls == []

    def test_ignores_a_host_github_shouldnt_return(self, tmp_path):
        calls: list = []
        resolver = copilot_relay.CopilotHostResolver(
            client=self._client(calls, payload={"endpoints": {"api": "https://evil.example"}}),
            cache_file=tmp_path / "missing.json",
        )

        assert resolver(f"Bearer {GITHUB_TOKEN}") is None

    def test_an_unreadable_cache_is_no_host(self, tmp_path):
        cache = tmp_path / "copilot-user-cache.json"
        cache.write_text("{not json")
        resolver = copilot_relay.CopilotHostResolver(
            client=self._client([], status=500), cache_file=cache
        )

        assert resolver(f"Bearer {GITHUB_TOKEN}") is None
