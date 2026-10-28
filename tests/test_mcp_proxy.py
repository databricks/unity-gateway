"""Tests for the `ug mcp-proxy` stdio<->streamable-HTTP bridge."""

from __future__ import annotations

import io
import json
import subprocess
import tomllib
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event

import anyio
import httpx
import pytest

from ucode import databricks as db_mod
from ucode import mcp_connection_login, mcp_proxy

WS = "https://example.databricks.com"
URL = f"{WS}/api/2.0/mcp/functions/system/ai"


def _runtime_dependencies() -> list[str]:
    project = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())[
        "project"
    ]
    return project["dependencies"]


def test_httpx_is_a_direct_runtime_dependency():
    assert any(dependency.startswith("httpx") for dependency in _runtime_dependencies())


def test_mcp_dependency_is_uncapped():
    # The proxy works against both mcp 1.x (httpx) and mcp 2.x (httpx2) via a
    # single code path (see mcp_proxy._httpx / _run), so `mcp` must NOT be capped
    # below 2 — a fresh install resolving mcp 2.x should work, not break (#307).
    mcp_specs = [d for d in _runtime_dependencies() if d.replace(" ", "").startswith("mcp")]
    assert mcp_specs, "mcp must be a direct dependency"
    assert not any("<2" in spec.replace(" ", "") for spec in mcp_specs), mcp_specs


def test_selected_httpx_matches_the_installed_mcp_sdk():
    # `_httpx()` must return the HTTP flavor the installed SDK is built on:
    # httpx2 for mcp 2.x, httpx for mcp 1.x. Using the wrong one makes the
    # transport reject our AsyncClient/Auth.
    from importlib.metadata import version

    from packaging.version import Version

    selected = mcp_proxy._httpx().__name__
    if Version(version("mcp")) >= Version("2"):
        assert selected == "httpx2"
    else:
        assert selected == "httpx"


def test_proxy_imports_the_streamable_http_client_shared_by_both_majors():
    # `streamable_http_client` (2.x-native, and re-exported by mcp 1.28+) is the
    # symbol the single code path relies on; a rename in a resolved SDK fails here.
    assert mcp_proxy.streamable_http_client is not None
    assert mcp_proxy.stdio_server is not None


class TestDatabricksTokenAuth:
    def test_injects_bearer_from_minted_token(self, monkeypatch):
        monkeypatch.setattr(mcp_proxy, "get_databricks_token", lambda ws, profile: "tok-123")
        auth = mcp_proxy._build_token_auth(URL, WS, "uc-dogfood")

        request = httpx.Request("POST", URL)
        self._drive(auth, request)

        assert request.headers["Authorization"] == "Bearer tok-123"

    def test_auth_is_an_instance_of_the_selected_httpx_auth(self, monkeypatch):
        # The auth must subclass the *same* httpx flavor's Auth as the transport,
        # or the SDK's AsyncClient won't accept it.
        monkeypatch.setattr(mcp_proxy, "get_databricks_token", lambda ws, profile: "t")
        auth = mcp_proxy._build_token_auth(URL, WS, None)

        assert isinstance(auth, mcp_proxy._httpx().Auth)

    def test_calls_get_token_with_workspace_and_profile(self, monkeypatch):
        calls: list[tuple[str, str | None]] = []
        monkeypatch.setattr(
            mcp_proxy,
            "get_databricks_token",
            lambda ws, profile: calls.append((ws, profile)) or "t",
        )
        auth = mcp_proxy._build_token_auth(URL, WS, "myprofile")

        self._drive(auth, httpx.Request("POST", URL))

        assert calls == [(WS, "myprofile")]

    def test_mints_a_fresh_token_per_request(self, monkeypatch):
        # Each request re-invokes get_databricks_token, so a rotated token is
        # picked up mid-session without the proxy tracking expiry itself.
        tokens = iter(["first", "second"])
        monkeypatch.setattr(mcp_proxy, "get_databricks_token", lambda ws, profile: next(tokens))
        auth = mcp_proxy._build_token_auth(URL, WS, None)

        r1 = httpx.Request("POST", URL)
        r2 = httpx.Request("POST", URL)
        self._drive(auth, r1)
        self._drive(auth, r2)

        assert r1.headers["Authorization"] == "Bearer first"
        assert r2.headers["Authorization"] == "Bearer second"

    def test_auth_flow_yields_the_same_request(self, monkeypatch):
        monkeypatch.setattr(mcp_proxy, "get_databricks_token", lambda ws, profile: "t")
        auth = mcp_proxy._build_token_auth(URL, WS, None)

        request = httpx.Request("POST", URL)
        yielded = self._drive(auth, request)

        assert yielded == [request]

    def test_dead_auth_becomes_a_terminal_proxy_auth_error(self, monkeypatch):
        # A raw RuntimeError escaping auth_flow tears through the transport's task
        # group and stalls the proxy until the client's startup timeout.
        # Translating it keeps the failure reportable by `serve`.
        def boom(ws, profile):
            raise RuntimeError("no access token; run `databricks auth login`")

        monkeypatch.setattr(mcp_proxy, "get_databricks_token", boom)
        auth = mcp_proxy._build_token_auth(URL, WS, "p")

        with pytest.raises(mcp_proxy.ProxyAuthError, match="databricks auth login"):
            self._drive(auth, httpx.Request("POST", URL))

    # --- lazy on-401 connection login ---------------------------------------

    @staticmethod
    def _drive(auth, request, *responses):
        """Drive the async auth flow with responses from the external HTTP boundary."""

        async def run():
            gen = auth.async_auth_flow(request)
            yielded: list = []
            try:
                yielded.append(await gen.__anext__())
                for response in responses or (None,):
                    yielded.append(await gen.asend(response))
            except StopAsyncIteration:
                pass
            finally:
                await gen.aclose()
            return yielded

        return anyio.run(run)

    @pytest.mark.parametrize(
        "method", ["initialize", "tools/list", "resources/list", "prompts/list", "ping"]
    )
    def test_discovery_401_does_not_start_connection_login(self, monkeypatch, method):
        token_calls = []
        logins = []
        monkeypatch.setattr(
            mcp_proxy,
            "get_databricks_token",
            lambda ws, profile, **kwargs: token_calls.append(kwargs) or "workspace-token",
        )
        monkeypatch.setattr(mcp_proxy, "mcp_service_needs_connection_login", lambda *a: True)
        monkeypatch.setattr(
            mcp_proxy,
            "run_connection_login",
            lambda *a, **k: logins.append(1) or (True, "signed in"),
        )
        auth = mcp_proxy._build_token_auth(CONN_URL, WS, "p")
        request = httpx.Request(
            "POST", CONN_URL, json={"jsonrpc": "2.0", "id": 1, "method": method}
        )

        yielded = self._drive(auth, request, httpx.Response(401))

        assert logins == []
        assert token_calls == [{}]
        assert len(yielded) == 1

    @pytest.mark.parametrize(
        ("method", "body"),
        [
            ("GET", b""),
            ("DELETE", b""),
            ("POST", b"not json"),
            ("POST", b"\xff"),
            ("POST", b"[]"),
            ("POST", b'{"jsonrpc":"2.0","method":"tools/call"}'),
        ],
    )
    def test_non_tool_http_requests_do_not_start_connection_login(self, monkeypatch, method, body):
        monkeypatch.setattr(mcp_proxy, "get_databricks_token", lambda *a, **k: "tok")
        monkeypatch.setattr(mcp_proxy, "mcp_service_needs_connection_login", lambda *a: True)
        logins = []
        monkeypatch.setattr(
            mcp_proxy,
            "run_connection_login",
            lambda *a, **k: logins.append(1) or (True, "signed in"),
        )
        auth = mcp_proxy._build_token_auth(CONN_URL, WS, "p")

        yielded = self._drive(
            auth,
            httpx.Request(method, CONN_URL, content=body),
            httpx.Response(401),
        )

        assert len(yielded) == 1
        assert logins == []

    @pytest.mark.parametrize("status", [403, 500])
    def test_non_401_tool_failures_do_not_start_connection_login(self, monkeypatch, status):
        token_calls = []
        logins = []
        monkeypatch.setattr(
            mcp_proxy,
            "get_databricks_token",
            lambda *a, **k: token_calls.append(k) or "tok",
        )
        monkeypatch.setattr(mcp_proxy, "mcp_service_needs_connection_login", lambda *a: True)
        monkeypatch.setattr(
            mcp_proxy,
            "run_connection_login",
            lambda *a, **k: logins.append(1) or (True, "signed in"),
        )
        request = httpx.Request(
            "POST", CONN_URL, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call"}
        )
        auth = mcp_proxy._build_token_auth(CONN_URL, WS, "p")

        yielded = self._drive(auth, request, httpx.Response(status))

        assert len(yielded) == 1
        assert token_calls == [{}]
        assert logins == []

    def test_on_401_from_connection_service_runs_login_then_retries(self, monkeypatch):
        monkeypatch.setattr(mcp_proxy, "get_databricks_token", lambda ws, profile, **k: "tok")
        monkeypatch.setattr(mcp_proxy, "mcp_service_needs_connection_login", lambda *a, **k: True)
        logins: list = []
        monkeypatch.setattr(
            mcp_proxy,
            "run_connection_login",
            lambda url, ws, **k: logins.append((url, k.get("profile"))) or (True, "signed in"),
        )
        auth = mcp_proxy._build_token_auth(CONN_URL, WS, "p")
        request = httpx.Request(
            "POST", CONN_URL, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call"}
        )
        yielded = self._drive(auth, request, httpx.Response(401))
        assert logins == [(CONN_URL, "p")]
        assert len(yielded) == 2
        assert yielded[1].headers["Authorization"] == "Bearer tok"

    def test_browser_connection_login_preserves_profile_and_retries_original_request(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.delenv("DATABRICKS_BEARER", raising=False)
        monkeypatch.delenv("DATABRICKS_BEARER_COMMAND", raising=False)
        monkeypatch.setattr(db_mod, "databricks_cli_path", lambda: "/opt/databricks/databricks")
        db_mod._remember_token(db_mod._token_memo_key(WS, "p"), "selected-user-token", 3600)
        config = tmp_path / "databrickscfg"
        original_config = f"[p]\nhost = {WS}\nauth_type = databricks-cli\nclient_id = keep-client\n"
        config.write_text(original_config)
        monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(config))
        monkeypatch.setattr(
            db_mod, "run", lambda *a, **k: pytest.fail("cached workspace token should be reused")
        )
        credential_requests = []
        browser_calls = []

        def urlopen(request, **kwargs):
            assert request.get_header("Authorization") == "Bearer selected-user-token"
            if request.full_url.endswith("/user-credentials"):
                credential_requests.append(request.full_url)
                body = {"provisioning_info": {"state": "ACTIVE" if browser_calls else "FAILED"}}
            else:
                body = {
                    "config": {
                        "source_connection": {
                            "securable_kind": "CONNECTION_SLACK_OAUTH_U2M_MAPPING"
                        }
                    }
                }
            response = io.BytesIO(json.dumps(body).encode())
            response.headers = {"X-Databricks-Org-Id": "12345"}
            return response

        def launch(argv, **kwargs):
            browser_calls.append(argv)
            assert "webbrowser.open" in argv[3]
            return subprocess.CompletedProcess(argv, 0)

        monkeypatch.setattr(db_mod.urllib_request, "urlopen", urlopen)
        monkeypatch.setattr(mcp_connection_login.subprocess_cross_os, "run", launch)
        auth = mcp_proxy._build_token_auth(CONN_URL, WS, "p")
        sent = []

        sdk_httpx = mcp_proxy._httpx()

        def upstream(request):
            sent.append((request.headers["Authorization"], request.content))
            return sdk_httpx.Response(401 if len(sent) == 1 else 200, json={"result": "ok"})

        async def scenario():
            async with sdk_httpx.AsyncClient(
                auth=auth, transport=sdk_httpx.MockTransport(upstream)
            ) as client:
                response = await client.post(CONN_URL, content=b'{"method":"tools/call","id":7}')
            assert response.json() == {"result": "ok"}

        anyio.run(scenario)

        body = b'{"method":"tools/call","id":7}'
        assert sent == [
            ("Bearer selected-user-token", body),
            ("Bearer selected-user-token", body),
        ]
        assert config.read_text() == original_config
        assert len(browser_calls) == 1
        assert len(credential_requests) == 2
        memo = db_mod._memoized_token(db_mod._token_memo_key(WS, "p"))
        assert memo is not None and memo.token == "selected-user-token"

    def test_non_connection_401_is_not_retried(self, monkeypatch):
        monkeypatch.setattr(mcp_proxy, "get_databricks_token", lambda ws, profile, **k: "tok")
        logins: list = []
        monkeypatch.setattr(
            mcp_proxy, "run_connection_login", lambda *a, **k: logins.append(1) or (True, "")
        )
        auth = mcp_proxy._build_token_auth(URL, WS, "p")  # URL is not an mcp-services endpoint
        yielded = self._drive(auth, httpx.Request("POST", URL), httpx.Response(401))
        assert logins == [] and len(yielded) == 1

    def test_success_response_never_logs_in(self, monkeypatch):
        monkeypatch.setattr(mcp_proxy, "get_databricks_token", lambda ws, profile: "tok")
        logins: list = []
        monkeypatch.setattr(
            mcp_proxy, "run_connection_login", lambda *a, **k: logins.append(1) or (True, "")
        )
        auth = mcp_proxy._build_token_auth(CONN_URL, WS, "p")
        yielded = self._drive(auth, httpx.Request("POST", CONN_URL), httpx.Response(200))
        assert logins == [] and len(yielded) == 1  # tools/list etc. never trigger a browser

    def test_no_login_service_does_not_drive_login_on_401(self, monkeypatch):
        # A no-login mcp-service (web_search) that 401s must NOT open a connection-login browser --
        # it's a bad-token problem, not a missing connection credential (AIGTWY-4856).
        monkeypatch.setattr(mcp_proxy, "get_databricks_token", lambda ws, profile, **k: "tok")
        monkeypatch.setattr(mcp_proxy, "mcp_service_needs_connection_login", lambda *a, **k: False)
        logins: list = []
        monkeypatch.setattr(
            mcp_proxy, "run_connection_login", lambda *a, **k: logins.append(1) or (True, "")
        )
        ws_url = f"{WS}/ai-gateway/mcp-services/system.ai.web_search"
        auth = mcp_proxy._build_token_auth(ws_url, WS, "p")
        request = httpx.Request(
            "POST", ws_url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call"}
        )
        yielded = self._drive(auth, request, httpx.Response(401))
        assert logins == [] and len(yielded) == 1

    def test_login_runs_at_most_once_per_session(self, monkeypatch):
        monkeypatch.setattr(mcp_proxy, "get_databricks_token", lambda ws, profile, **k: "tok")
        monkeypatch.setattr(mcp_proxy, "mcp_service_needs_connection_login", lambda *a, **k: True)
        logins: list = []
        monkeypatch.setattr(
            mcp_proxy, "run_connection_login", lambda *a, **k: logins.append(1) or (True, "")
        )
        auth = mcp_proxy._build_token_auth(CONN_URL, WS, "p")
        for request_id in [1, 2]:
            request = httpx.Request(
                "POST",
                CONN_URL,
                json={"jsonrpc": "2.0", "id": request_id, "method": "tools/call"},
            )
            self._drive(auth, request, httpx.Response(401))
        assert logins == [1]

    def test_use_pat_never_drives_connection_login(self, monkeypatch):
        monkeypatch.setattr(mcp_proxy, "get_databricks_token", lambda ws, profile: "tok")
        logins: list = []
        monkeypatch.setattr(
            mcp_proxy, "run_connection_login", lambda *a, **k: logins.append(1) or (True, "")
        )
        auth = mcp_proxy._build_token_auth(CONN_URL, WS, "p", use_pat=True)
        yielded = self._drive(auth, httpx.Request("POST", CONN_URL), httpx.Response(401))
        assert logins == [] and len(yielded) == 1  # PAT has no connection OAuth to drive

    def test_login_failure_becomes_a_proxy_auth_error(self, monkeypatch):
        monkeypatch.setattr(mcp_proxy, "get_databricks_token", lambda ws, profile, **k: "tok")
        monkeypatch.setattr(mcp_proxy, "mcp_service_needs_connection_login", lambda *a, **k: True)
        monkeypatch.setattr(
            mcp_proxy, "run_connection_login", lambda *a, **k: (False, "user cancelled")
        )
        auth = mcp_proxy._build_token_auth(CONN_URL, WS, "p")
        request = httpx.Request(
            "POST", CONN_URL, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call"}
        )
        with pytest.raises(mcp_proxy.ProxyAuthError, match="user cancelled"):
            self._drive(auth, request, httpx.Response(401))

    def test_login_failure_requires_explicit_retry(self, monkeypatch):
        monkeypatch.setattr(mcp_proxy, "get_databricks_token", lambda ws, profile: "tok")
        monkeypatch.setattr(mcp_proxy, "mcp_service_needs_connection_login", lambda *a, **k: True)
        outcomes = iter([(False, "user cancelled"), (True, "signed in")])
        monkeypatch.setattr(mcp_proxy, "run_connection_login", lambda *a, **k: next(outcomes))
        login = mcp_proxy._ConnectionLogin(CONN_URL, WS, "p")

        with pytest.raises(mcp_proxy.ProxyAuthError, match="user cancelled"):
            login.run_if_needed()

        assert not login.run_if_needed()
        assert login.authenticate_or_confirm_ready()

    def test_token_lookup_failure_can_be_retried(self, monkeypatch):
        tokens = iter([RuntimeError("token unavailable"), "tok"])

        def get_token(*args):
            token = next(tokens)
            if isinstance(token, Exception):
                raise token
            return token

        monkeypatch.setattr(mcp_proxy, "get_databricks_token", get_token)
        monkeypatch.setattr(mcp_proxy, "mcp_service_needs_connection_login", lambda *a, **k: True)
        logins = []
        monkeypatch.setattr(
            mcp_proxy,
            "run_connection_login",
            lambda *a, **k: logins.append(1) or (True, "signed in"),
        )
        login = mcp_proxy._ConnectionLogin(CONN_URL, WS, "p")

        assert not login.run_if_needed()
        assert login.run_if_needed()
        assert logins == [1]

    def test_explicit_authentication_retries_after_success_and_cancellation(self, monkeypatch):
        monkeypatch.setattr(mcp_proxy, "get_databricks_token", lambda *a: "tok")
        monkeypatch.setattr(mcp_proxy, "mcp_service_needs_connection_login", lambda *a: True)
        logins = []
        outcomes = iter([(True, "signed in"), (False, "user cancelled"), (True, "signed in")])

        def login(*args, **kwargs):
            logins.append(1)
            return next(outcomes)

        monkeypatch.setattr(mcp_proxy, "run_connection_login", login)
        connection_login = mcp_proxy._ConnectionLogin(CONN_URL, WS, "p")
        assert connection_login.run_if_needed()
        with pytest.raises(mcp_proxy.ProxyAuthError, match="user cancelled"):
            connection_login.authenticate_or_confirm_ready()
        assert not connection_login.run_if_needed()
        assert connection_login.authenticate_or_confirm_ready()
        assert len(logins) == 3

    def test_async_auth_flow_drives_login_on_401(self, monkeypatch):
        monkeypatch.setattr(mcp_proxy, "get_databricks_token", lambda ws, profile, **k: "tok")
        monkeypatch.setattr(mcp_proxy, "mcp_service_needs_connection_login", lambda *a, **k: True)
        logins: list = []
        monkeypatch.setattr(
            mcp_proxy, "run_connection_login", lambda *a, **k: logins.append(1) or (True, "")
        )
        httpx_module = mcp_proxy._httpx()
        bodies = []
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "query"}}

        def upstream(request):
            bodies.append(json.loads(request.content))
            return httpx_module.Response(401 if len(bodies) == 1 else 200, json={"ok": True})

        async def scenario():
            async with httpx_module.AsyncClient(
                auth=mcp_proxy._build_token_auth(CONN_URL, WS, "p"),
                transport=httpx_module.MockTransport(upstream),
            ) as client:
                return await client.post(CONN_URL, json=body)

        response = anyio.run(scenario)

        assert response.status_code == 200
        assert bodies == [body, body]
        assert logins == [1]

    def test_sync_auth_flow_is_rejected(self, monkeypatch):
        # The proxy is async-only; a sync client must fail loudly rather than silently skip the
        # bearer via httpx's no-op default flow.
        monkeypatch.setattr(mcp_proxy, "get_databricks_token", lambda ws, profile: "t")
        auth = mcp_proxy._build_token_auth(URL, WS, None)

        with pytest.raises(mcp_proxy.ProxyAuthError, match="async-only"):
            list(auth.sync_auth_flow(httpx.Request("POST", URL)))


CONN_URL = f"{WS}/ai-gateway/mcp-services/system.ai.github"


class TestMcpBridgeConnectionLogin:
    @staticmethod
    def _login_url_failure(kind, service="google_calendar", request_id=1, login_url=None):
        from mcp.types import ErrorData

        login_url = login_url or f"{WS}/mcp-service-login?name=system.ai.{service}&o=123"
        text = (
            f"{service} is not connected for this user, or its credential has expired or "
            f"been revoked. Do not retry this tool or call any other {service} tool: every "
            "call will fail the same way until the user reconnects. Stop and tell the user "
            "exactly how to connect, then continue once they confirm it is connected. "
            f"Open {login_url} and sign in."
        )
        if kind == "tool-error":
            payload = mcp_proxy.JSONRPCResponse(
                jsonrpc="2.0",
                id=request_id,
                result={"isError": True, "content": [{"type": "text", "text": text}]},
            )
        else:
            payload = mcp_proxy.JSONRPCError(
                jsonrpc="2.0",
                id=request_id,
                error=ErrorData(
                    code=-32042,
                    message="Sign-in required",
                    data={
                        "elicitations": [
                            {"mode": "url", "url": login_url, "elicitationId": "sign-in"}
                        ]
                    },
                ),
            )
        return mcp_proxy.SessionMessage(payload)

    @pytest.mark.parametrize(
        ("service", "kind"), [("gmail", "tool-error"), ("google_calendar", "elicitation")]
    )
    @pytest.mark.parametrize("outcome", ["success", "canceled", "still-unauthenticated"])
    def test_login_url_failure_runs_login_and_retries_once(
        self, monkeypatch, service, kind, outcome
    ):
        monkeypatch.setattr(mcp_proxy, "get_databricks_token", lambda *a: "tok")
        monkeypatch.setattr(mcp_proxy, "mcp_service_needs_connection_login", lambda *a: True)
        logins = []
        url = f"{WS}/ai-gateway/mcp-services/system.ai.{service}"
        monkeypatch.setattr(
            mcp_proxy,
            "run_connection_login",
            lambda *a, **k: logins.append((a, k)) or (outcome != "canceled", "canceled"),
        )

        async def scenario():
            source_send, source_read = anyio.create_memory_object_stream(2)
            dest_send, dest_read = anyio.create_memory_object_stream(2)
            retry_send, retry_read = anyio.create_memory_object_stream(2)
            bridge = mcp_proxy._McpBridge(mcp_proxy._ConnectionLogin(url, WS, "p"))
            request = self._tool_call_request(name=f"{service}_list")
            bridge.pending_requests[1] = request
            failure = self._login_url_failure(kind, service)
            async with anyio.create_task_group() as tg:
                tg.start_soon(bridge.pump_upstream, source_read, dest_send, retry_send)
                with anyio.fail_after(1):
                    await source_send.send(failure)
                    if outcome == "canceled":
                        assert await dest_read.receive() is failure
                    else:
                        assert await retry_read.receive() is request
                        with pytest.raises(anyio.WouldBlock):
                            dest_read.receive_nowait()
                        result = (
                            self._login_url_failure(kind, service)
                            if outcome == "still-unauthenticated"
                            else mcp_proxy.SessionMessage(
                                mcp_proxy.JSONRPCResponse(
                                    jsonrpc="2.0", id=1, result={"content": []}
                                )
                            )
                        )
                        await source_send.send(result)
                        assert await dest_read.receive() is result
                    with pytest.raises(anyio.WouldBlock):
                        retry_read.receive_nowait()
                tg.cancel_scope.cancel()

        anyio.run(scenario)
        assert logins == [((url, WS), {"profile": "p"})]

    @pytest.mark.parametrize("kind", ["tool-error", "elicitation"])
    @pytest.mark.parametrize("classification", ["pat", False, None])
    def test_login_url_tool_failure_without_supported_login_is_unchanged(
        self, monkeypatch, kind, classification
    ):
        monkeypatch.setattr(mcp_proxy, "get_databricks_token", lambda *a: "tok")
        monkeypatch.setattr(
            mcp_proxy, "mcp_service_needs_connection_login", lambda *a: classification
        )
        monkeypatch.setattr(
            mcp_proxy, "run_connection_login", lambda *a, **k: pytest.fail("unexpected login")
        )

        async def scenario():
            source_send, source_read = anyio.create_memory_object_stream(1)
            dest_send, dest_read = anyio.create_memory_object_stream(1)
            retry_send, retry_read = anyio.create_memory_object_stream(1)
            url = f"{WS}/ai-gateway/mcp-services/system.ai.google_calendar"
            bridge = mcp_proxy._McpBridge(
                mcp_proxy._ConnectionLogin(url, WS, "p", use_pat=classification == "pat")
            )
            bridge.pending_requests[1] = self._tool_call_request()
            failure = self._login_url_failure(kind)
            async with anyio.create_task_group() as tg:
                tg.start_soon(bridge.pump_upstream, source_read, dest_send, retry_send)
                with anyio.fail_after(1):
                    await source_send.send(failure)
                    assert await dest_read.receive() is failure
                    with pytest.raises(anyio.WouldBlock):
                        retry_read.receive_nowait()
                tg.cancel_scope.cancel()

        anyio.run(scenario)

    @pytest.mark.parametrize("kind", ["tool-error", "elicitation"])
    @pytest.mark.parametrize(
        "login_url",
        [
            "https://other.example/mcp-service-login?name=system.ai.google_calendar",
            f"{WS}/mcp-service-login?name=system.ai.gmail",
            f"{WS}/mcp-service-login?name=system.ai.google_calendar&name=system.ai.gmail",
            f"{WS}/mcp-service-login?name=system.ai.google_calendar#other",
            f"{WS}/mcp-service-login-extra?name=system.ai.google_calendar",
            f"{WS}/mcp-approval?name=system.ai.google_calendar",
            "https://example.databricks.com@other.example/mcp-service-login?name=system.ai.google_calendar",
            "http://example.databricks.com/mcp-service-login?name=system.ai.google_calendar",
            "https://[invalid/mcp-service-login?name=system.ai.google_calendar",
        ],
    )
    def test_other_login_and_approval_urls_do_not_start_authentication(
        self, monkeypatch, kind, login_url
    ):
        monkeypatch.setattr(mcp_proxy, "get_databricks_token", lambda *a: "tok")
        monkeypatch.setattr(mcp_proxy, "mcp_service_needs_connection_login", lambda *a: True)
        monkeypatch.setattr(
            mcp_proxy, "run_connection_login", lambda *a, **k: pytest.fail("unexpected login")
        )

        async def scenario():
            source_send, source_read = anyio.create_memory_object_stream(1)
            dest_send, dest_read = anyio.create_memory_object_stream(1)
            retry_send, retry_read = anyio.create_memory_object_stream(1)
            url = f"{WS}/ai-gateway/mcp-services/system.ai.google_calendar"
            bridge = mcp_proxy._McpBridge(mcp_proxy._ConnectionLogin(url, WS, "p"))
            bridge.pending_requests[1] = self._tool_call_request()
            failure = self._login_url_failure(kind, login_url=login_url)
            async with anyio.create_task_group() as tg:
                tg.start_soon(bridge.pump_upstream, source_read, dest_send, retry_send)
                with anyio.fail_after(1):
                    await source_send.send(failure)
                    assert await dest_read.receive() is failure
                    with pytest.raises(anyio.WouldBlock):
                        retry_read.receive_nowait()
                tg.cancel_scope.cancel()

        anyio.run(scenario)

    @pytest.mark.parametrize(
        "case", ["success", "missing-error-marker", "nontext", "mixed-elicitation", "other-code"]
    )
    def test_login_links_outside_auth_failures_are_unchanged(self, monkeypatch, case):
        monkeypatch.setattr(mcp_proxy, "get_databricks_token", lambda *a: "tok")
        monkeypatch.setattr(mcp_proxy, "mcp_service_needs_connection_login", lambda *a: True)
        monkeypatch.setattr(
            mcp_proxy, "run_connection_login", lambda *a, **k: pytest.fail("unexpected login")
        )
        failure = self._login_url_failure(
            "elicitation" if case in {"mixed-elicitation", "other-code"} else "tool-error"
        )
        payload = failure.message
        if isinstance(payload, mcp_proxy.JSONRPCResponse):
            if case == "success":
                payload.result["isError"] = False
            elif case == "missing-error-marker":
                payload.result.pop("isError")
            else:
                payload.result["content"][0]["type"] = "image"
        elif case == "other-code":
            payload.error.code = -32002
        else:
            payload.error.data["elicitations"].append(
                {"mode": "url", "url": f"{WS}/mcp-approval", "elicitationId": "approval"}
            )

        async def scenario():
            source_send, source_read = anyio.create_memory_object_stream(1)
            dest_send, dest_read = anyio.create_memory_object_stream(1)
            retry_send, retry_read = anyio.create_memory_object_stream(1)
            url = f"{WS}/ai-gateway/mcp-services/system.ai.google_calendar"
            bridge = mcp_proxy._McpBridge(mcp_proxy._ConnectionLogin(url, WS, "p"))
            bridge.pending_requests[1] = self._tool_call_request()
            async with anyio.create_task_group() as tg:
                tg.start_soon(bridge.pump_upstream, source_read, dest_send, retry_send)
                with anyio.fail_after(1):
                    await source_send.send(failure)
                    assert await dest_read.receive() is failure
                    with pytest.raises(anyio.WouldBlock):
                        retry_read.receive_nowait()
                tg.cancel_scope.cancel()

        anyio.run(scenario)

    @pytest.mark.parametrize("next_operation", ["discovery", "tool-call"])
    def test_metadata_failure_during_discovery_does_not_disable_later_recovery(
        self, monkeypatch, next_operation
    ):
        metadata = iter(
            [
                (None, "HTTP 503"),
                (
                    {"config": {"source_connection": {"securable_kind": "CONNECTION_HTTP_DCR"}}},
                    None,
                ),
            ]
        )
        monkeypatch.setattr(db_mod, "_http_get_json", lambda *a: next(metadata))
        monkeypatch.setattr(mcp_proxy, "get_databricks_token", lambda *a: "tok")
        logins = []
        monkeypatch.setattr(
            mcp_proxy, "run_connection_login", lambda *a, **k: logins.append(1) or (True, "")
        )

        async def scenario():
            source_send, source_read = anyio.create_memory_object_stream(2)
            dest_send, dest_read = anyio.create_memory_object_stream(2)
            retry_send, retry_read = anyio.create_memory_object_stream(2)
            bridge = mcp_proxy._McpBridge(mcp_proxy._ConnectionLogin(CONN_URL, WS, "p"))
            bridge.pending_requests[1] = self._request(1)
            next_request = (
                self._request(2) if next_operation == "discovery" else self._tool_call_request(2)
            )
            bridge.pending_requests[2] = next_request
            async with anyio.create_task_group() as tg:
                tg.start_soon(bridge.pump_upstream, source_read, dest_send, retry_send)
                with anyio.fail_after(1):
                    initial = self._response(1)
                    await source_send.send(initial)
                    assert await dest_read.receive() is initial
                    assert logins == []
                    if next_operation == "discovery":
                        bridge.connection_login._metadata_retry_at = 0.0
                        await source_send.send(self._response(2))
                        received = (await dest_read.receive()).message
                        assert received.result["tools"] == [bridge._authentication_tool()]
                        assert logins == []
                    else:
                        await source_send.send(self._error(request_id=2))
                        assert await retry_read.receive() is next_request
                        success = self._response(2)
                        await source_send.send(success)
                        assert await dest_read.receive() is success
                        assert logins == [1]
                tg.cancel_scope.cancel()

        anyio.run(scenario)

    def test_slow_discovery_metadata_does_not_block_other_responses(self, monkeypatch):
        started, release = Event(), Event()
        monkeypatch.setattr(mcp_proxy, "get_databricks_token", lambda *a: "tok")

        def metadata(*args):
            started.set()
            assert release.wait(2), "metadata boundary was not released"
            return True

        monkeypatch.setattr(mcp_proxy, "mcp_service_needs_connection_login", metadata)

        async def scenario():
            source_send, source_read = anyio.create_memory_object_stream(2)
            dest_send, dest_read = anyio.create_memory_object_stream(2)
            retry_send, _ = anyio.create_memory_object_stream(2)
            bridge = mcp_proxy._McpBridge(mcp_proxy._ConnectionLogin(CONN_URL, WS, "p"))
            bridge.pending_requests = {1: self._request(1), 2: self._tool_call_request(2)}
            async with anyio.create_task_group() as tg:
                tg.start_soon(bridge.pump_upstream, source_read, dest_send, retry_send)
                try:
                    with anyio.fail_after(1):
                        await source_send.send(self._response(1))
                        assert await anyio.to_thread.run_sync(started.wait, 1)
                        other = self._response(2)
                        await source_send.send(other)
                        assert await dest_read.receive() is other
                        release.set()
                        assert (await dest_read.receive()).message.result["tools"] == [
                            bridge._authentication_tool()
                        ]
                finally:
                    release.set()
                    tg.cancel_scope.cancel()

        anyio.run(scenario)

    @pytest.mark.parametrize("failure", ["token", "metadata"])
    def test_failed_discovery_has_retry_cooldown(self, monkeypatch, failure):
        calls = []

        def token(*args):
            if failure == "token":
                calls.append(1)
                raise RuntimeError("token unavailable")
            return "tok"

        monkeypatch.setattr(mcp_proxy, "get_databricks_token", token)
        monkeypatch.setattr(
            mcp_proxy, "mcp_service_needs_connection_login", lambda *a: calls.append(1)
        )
        login = mcp_proxy._ConnectionLogin(CONN_URL, WS, "p")
        assert not login.supports_login()
        assert not login.supports_login()
        assert len(calls) == 1
        login._metadata_retry_at = 0.0
        assert not login.supports_login()
        assert len(calls) == 2

    @pytest.mark.parametrize("normal_tools", [[], [{"name": "search_pull_requests"}], [None]])
    def test_successful_discovery_keeps_authentication_available(self, monkeypatch, normal_tools):
        from mcp.shared.message import ClientMessageMetadata

        lookups = []
        monkeypatch.setattr(mcp_proxy, "get_databricks_token", lambda *a: "tok")
        monkeypatch.setattr(
            mcp_proxy,
            "mcp_service_needs_connection_login",
            lambda *a: lookups.append(a) or True,
        )
        monkeypatch.setattr(
            mcp_proxy,
            "run_connection_login",
            lambda *a, **k: pytest.fail("discovery must not open a browser"),
        )

        async def scenario():
            source_send, source_read = anyio.create_memory_object_stream(2)
            dest_send, dest_read = anyio.create_memory_object_stream(2)
            retry_send, _ = anyio.create_memory_object_stream(2)
            bridge = mcp_proxy._McpBridge(mcp_proxy._ConnectionLogin(CONN_URL, WS, "p"))
            async with anyio.create_task_group() as tg:
                tg.start_soon(bridge.pump_upstream, source_read, dest_send, retry_send)
                with anyio.fail_after(1):
                    for request_id in (1, 2):
                        bridge.pending_requests[request_id] = self._request(request_id)
                        response = self._response(request_id)
                        response.message.result.update(tools=normal_tools, _meta={"marker": "kept"})
                        response.metadata = ClientMessageMetadata(resumption_token="resume")
                        await source_send.send(response)
                        received = await dest_read.receive()
                        assert received.message.result == {
                            "tools": [*normal_tools, bridge._authentication_tool()],
                            "_meta": {"marker": "kept"},
                        }
                        assert received.metadata is response.metadata
                        assert response.message.result["tools"] == normal_tools
                tg.cancel_scope.cancel()

        anyio.run(scenario)
        assert len(lookups) == 1

    @pytest.mark.parametrize("mode", ["non-u2m", "pat", "metadata-failure", "token-failure"])
    def test_discovery_without_supported_login_is_unchanged(self, monkeypatch, mode):
        lookups = []

        def token(*args):
            if mode == "token-failure":
                raise RuntimeError("token unavailable")
            return "tok"

        monkeypatch.setattr(mcp_proxy, "get_databricks_token", token)
        if mode == "metadata-failure":
            monkeypatch.setattr(db_mod, "_http_get_json", lambda *a: (None, "metadata unavailable"))
        else:
            monkeypatch.setattr(
                mcp_proxy,
                "mcp_service_needs_connection_login",
                lambda *a: lookups.append(1) or mode == "pat",
            )
        monkeypatch.setattr(
            mcp_proxy, "run_connection_login", lambda *a, **k: pytest.fail("unexpected login")
        )

        async def scenario():
            source_send, source_read = anyio.create_memory_object_stream(1)
            dest_send, dest_read = anyio.create_memory_object_stream(1)
            retry_send, _ = anyio.create_memory_object_stream(1)
            bridge = mcp_proxy._McpBridge(
                mcp_proxy._ConnectionLogin(CONN_URL, WS, "p", use_pat=mode == "pat")
            )
            async with anyio.create_task_group() as tg:
                tg.start_soon(bridge.pump_upstream, source_read, dest_send, retry_send)
                with anyio.fail_after(1):
                    for request_id in (1, 2):
                        bridge.pending_requests[request_id] = self._request(request_id)
                        response = self._response(request_id)
                        await source_send.send(response)
                        assert await dest_read.receive() is response
                tg.cancel_scope.cancel()

        anyio.run(scenario)
        assert lookups == ([1] if mode == "non-u2m" else [])

    @pytest.mark.parametrize("kind", ["auth-code", "elicitation"])
    def test_upstream_authenticate_tool_is_not_shadowed(self, monkeypatch, kind):
        monkeypatch.setattr(mcp_proxy, "get_databricks_token", lambda *a: "tok")
        monkeypatch.setattr(mcp_proxy, "mcp_service_needs_connection_login", lambda *a: True)
        monkeypatch.setattr(
            mcp_proxy, "run_connection_login", lambda *a, **k: pytest.fail("unexpected login")
        )

        async def scenario():
            client_send, client_read = anyio.create_memory_object_stream(2)
            source_send, source_read = anyio.create_memory_object_stream(2)
            dest_send, dest_read = anyio.create_memory_object_stream(2)
            retry_send, retry_read = anyio.create_memory_object_stream(2)
            bridge = mcp_proxy._McpBridge(mcp_proxy._ConnectionLogin(CONN_URL, WS, "p"))
            bridge.pending_requests[1] = self._request()
            response = self._response()
            response.message.result["tools"] = [{"name": "authenticate"}]
            async with anyio.create_task_group() as tg:
                tg.start_soon(bridge.pump_upstream, source_read, dest_send, retry_send)
                tg.start_soon(bridge.pump_client, client_read, retry_send, dest_send)
                with anyio.fail_after(1):
                    await source_send.send(response)
                    assert await dest_read.receive() is response
                    request = self._tool_call_request(2, "authenticate")
                    await client_send.send(request)
                    assert await retry_read.receive() is request
                    failure = (
                        self._error(request_id=2)
                        if kind == "auth-code"
                        else self._login_url_failure(kind, service="github", request_id=2)
                    )
                    await source_send.send(failure)
                    assert await dest_read.receive() is failure
                    with pytest.raises(anyio.WouldBlock):
                        retry_read.receive_nowait()
                tg.cancel_scope.cancel()

        anyio.run(scenario)

    def test_upstream_authenticate_removed_on_refresh_restores_local_recovery(self, monkeypatch):
        monkeypatch.setattr(mcp_proxy, "get_databricks_token", lambda *a: "tok")
        monkeypatch.setattr(mcp_proxy, "mcp_service_needs_connection_login", lambda *a: True)
        logins = []
        monkeypatch.setattr(
            mcp_proxy, "run_connection_login", lambda *a, **k: logins.append(1) or (True, "")
        )

        async def scenario():
            source_send, source_read = anyio.create_memory_object_stream(2)
            dest_send, dest_read = anyio.create_memory_object_stream(2)
            retry_send, retry_read = anyio.create_memory_object_stream(2)
            bridge = mcp_proxy._McpBridge(mcp_proxy._ConnectionLogin(CONN_URL, WS, "p"))
            query = {"name": "query"}

            async with anyio.create_task_group() as tg:
                tg.start_soon(bridge.pump_upstream, source_read, dest_send, retry_send)
                with anyio.fail_after(2):
                    bridge.pending_requests[1] = self._request(1)
                    first = self._response(1)
                    first.message.result["tools"] = [{"name": "authenticate"}, query]
                    await source_send.send(first)
                    assert await dest_read.receive() is first

                    bridge.pending_requests[2] = self._request(2)
                    refreshed = self._response(2)
                    refreshed.message.result["tools"] = [query]
                    await source_send.send(refreshed)
                    received = await dest_read.receive()
                    assert received.message.result["tools"] == [
                        query,
                        bridge._authentication_tool(),
                    ]
                    assert logins == []

                    request = self._tool_call_request(3, "query")
                    bridge.pending_requests[3] = request
                    await source_send.send(self._error(request_id=3))
                    assert await retry_read.receive() is request
                    assert logins == [1]
                    success = mcp_proxy.SessionMessage(
                        mcp_proxy.JSONRPCResponse(jsonrpc="2.0", id=3, result={"content": []})
                    )
                    await source_send.send(success)
                    assert await dest_read.receive() is success
                tg.cancel_scope.cancel()

        anyio.run(scenario)

    @pytest.mark.parametrize("upstream_authenticate", [False, True])
    def test_paginated_discovery_adds_local_auth_only_on_final_page(
        self, monkeypatch, upstream_authenticate
    ):
        monkeypatch.setattr(mcp_proxy, "get_databricks_token", lambda *a: "tok")
        monkeypatch.setattr(mcp_proxy, "mcp_service_needs_connection_login", lambda *a: True)

        async def scenario():
            source_send, source_read = anyio.create_memory_object_stream(2)
            dest_send, dest_read = anyio.create_memory_object_stream(2)
            retry_send, _ = anyio.create_memory_object_stream(2)
            bridge = mcp_proxy._McpBridge(mcp_proxy._ConnectionLogin(CONN_URL, WS, "p"))
            async with anyio.create_task_group() as tg:
                tg.start_soon(bridge.pump_upstream, source_read, dest_send, retry_send)
                with anyio.fail_after(1):
                    bridge.pending_requests[1] = self._request(1)
                    first = self._response(1)
                    first.message.result["nextCursor"] = "next-page"
                    if upstream_authenticate:
                        first.message.result["tools"] = [{"name": "authenticate"}]
                    await source_send.send(first)
                    assert await dest_read.receive() is first
                    bridge.pending_requests[2] = self._request(2)
                    bridge.pending_requests[2].message.params = {"cursor": "next-page"}
                    final = self._response(2)
                    await source_send.send(final)
                    received = await dest_read.receive()
                    if upstream_authenticate:
                        assert received is final
                    else:
                        assert received.message.result["tools"] == [bridge._authentication_tool()]
                tg.cancel_scope.cancel()

        anyio.run(scenario)

    @pytest.mark.parametrize(
        "failure_message",
        [
            "Sign-in required",
            'Atlassian tool error: {"error":true,"message":"You don\'t have permission to '
            'connect from this IP address. Please ask your organization admin for access."}',
        ],
    )
    def test_tool_failure_explicit_login_and_later_reauthentication(
        self, monkeypatch, failure_message
    ):
        monkeypatch.setattr(mcp_proxy, "get_databricks_token", lambda *a: "tok")
        monkeypatch.setattr(mcp_proxy, "mcp_service_needs_connection_login", lambda *a: True)
        logins = []
        monkeypatch.setattr(
            mcp_proxy, "run_connection_login", lambda *a, **k: logins.append(1) or (True, "")
        )

        async def scenario():
            client_send, client_read = anyio.create_memory_object_stream(10)
            source_send, source_read = anyio.create_memory_object_stream(10)
            dest_send, dest_read = anyio.create_memory_object_stream(10)
            retry_send, retry_read = anyio.create_memory_object_stream(10)
            bridge = mcp_proxy._McpBridge(mcp_proxy._ConnectionLogin(CONN_URL, WS, "p"))
            async with anyio.create_task_group() as tg:
                tg.start_soon(bridge.pump_upstream, source_read, dest_send, retry_send)
                tg.start_soon(bridge.pump_client, client_read, retry_send, dest_send)
                with anyio.fail_after(2):
                    discovery = self._request(1)
                    await client_send.send(discovery)
                    assert await retry_read.receive() is discovery
                    await source_send.send(self._response(1))
                    assert (await dest_read.receive()).message.result["tools"][0][
                        "name"
                    ] == "authenticate"
                    for attempt in (1, 2):
                        call_id = attempt * 10
                        request = self._tool_call_request(call_id)
                        await client_send.send(request)
                        assert await retry_read.receive() is request
                        failure = mcp_proxy.SessionMessage(
                            mcp_proxy.JSONRPCResponse(
                                jsonrpc="2.0",
                                id=call_id,
                                result={
                                    "isError": True,
                                    "content": [{"type": "text", "text": failure_message}],
                                },
                            )
                        )
                        await source_send.send(failure)
                        assert await dest_read.receive() is failure
                        assert len(logins) == attempt - 1
                        await client_send.send(self._tool_call_request(call_id + 1, "authenticate"))
                        assert (
                            await dest_read.receive()
                        ).message.method == "notifications/tools/list_changed"
                        refresh = self._request(call_id + 2)
                        await client_send.send(refresh)
                        assert await retry_read.receive() is refresh
                        await source_send.send(self._response(call_id + 2))
                        assert (await dest_read.receive()).message.result["tools"][0][
                            "name"
                        ] == "authenticate"
                        authentication = (await dest_read.receive()).message
                        assert authentication.id == call_id + 1
                        assert not authentication.result["isError"]
                        assert len(logins) == attempt
                        retry = self._tool_call_request(call_id + 3)
                        await client_send.send(retry)
                        assert await retry_read.receive() is retry
                        success = mcp_proxy.SessionMessage(
                            mcp_proxy.JSONRPCResponse(
                                jsonrpc="2.0", id=call_id + 3, result={"content": []}
                            )
                        )
                        await source_send.send(success)
                        assert await dest_read.receive() is success
                tg.cancel_scope.cancel()

        anyio.run(scenario)

    @staticmethod
    def _request(request_id=1):
        return mcp_proxy.SessionMessage(
            mcp_proxy.JSONRPCRequest(
                jsonrpc="2.0",
                id=request_id,
                method="tools/list",
            )
        )

    @staticmethod
    def _tool_call_request(request_id=1, name="search_pull_requests"):
        return mcp_proxy.SessionMessage(
            mcp_proxy.JSONRPCRequest(
                jsonrpc="2.0",
                id=request_id,
                method="tools/call",
                params={"name": name, "arguments": {}},
            )
        )

    @staticmethod
    def _error(code=mcp_proxy.CONNECTION_AUTH_REQUIRED_ERROR_CODE, request_id=1):
        from mcp.types import ErrorData

        return mcp_proxy.SessionMessage(
            mcp_proxy.JSONRPCError(
                jsonrpc="2.0",
                id=request_id,
                error=ErrorData(code=code, message="authentication required"),
            )
        )

    @staticmethod
    def _response(request_id=1):
        return mcp_proxy.SessionMessage(
            mcp_proxy.JSONRPCResponse(
                jsonrpc="2.0",
                id=request_id,
                result={"tools": []},
            )
        )

    def test_auth_error_runs_login_replays_request_and_suppresses_first_error(self, monkeypatch):
        monkeypatch.setattr(mcp_proxy, "get_databricks_token", lambda ws, profile: "tok")
        monkeypatch.setattr(mcp_proxy, "mcp_service_needs_connection_login", lambda *a: True)
        logins: list[tuple[str, str | None]] = []
        monkeypatch.setattr(
            mcp_proxy,
            "run_connection_login",
            lambda url, ws, **kwargs: (
                logins.append((url, kwargs.get("profile"))) or (True, "signed in")
            ),
        )

        async def scenario():
            client_send, client_read = anyio.create_memory_object_stream(10)
            to_client, client_receive = anyio.create_memory_object_stream(10)
            from_upstream, upstream_read = anyio.create_memory_object_stream(10)
            to_upstream, upstream_receive = anyio.create_memory_object_stream(10)
            bridge = mcp_proxy._McpBridge(mcp_proxy._ConnectionLogin(CONN_URL, WS, "p"))
            request = self._tool_call_request()
            response = self._response()

            async with anyio.create_task_group() as tg:
                tg.start_soon(bridge.pump_client, client_read, to_upstream)
                tg.start_soon(bridge.pump_upstream, upstream_read, to_client, to_upstream)
                with anyio.fail_after(1):
                    await client_send.send(request)
                    assert await upstream_receive.receive() is request
                    await from_upstream.send(self._error())
                    assert await upstream_receive.receive() is request
                    with pytest.raises(anyio.WouldBlock):
                        client_receive.receive_nowait()
                    await from_upstream.send(response)
                    assert await client_receive.receive() is response
                tg.cancel_scope.cancel()

        anyio.run(scenario)
        assert logins == [(CONN_URL, "p")]

    def test_non_tool_auth_error_is_forwarded_without_login(self, monkeypatch):
        monkeypatch.setattr(
            mcp_proxy,
            "run_connection_login",
            lambda *a, **k: pytest.fail("non-tool requests must not open a browser"),
        )

        async def scenario():
            from_upstream, upstream_read = anyio.create_memory_object_stream(1)
            to_client, client_receive = anyio.create_memory_object_stream(1)
            to_upstream, upstream_receive = anyio.create_memory_object_stream(1)
            bridge = mcp_proxy._McpBridge(mcp_proxy._ConnectionLogin(CONN_URL, WS, "p"))
            bridge.pending_requests[1] = mcp_proxy.SessionMessage(
                mcp_proxy.JSONRPCRequest(
                    jsonrpc="2.0",
                    id=1,
                    method="resources/list",
                )
            )
            error = self._error()

            async with anyio.create_task_group() as tg:
                tg.start_soon(bridge.pump_upstream, upstream_read, to_client, to_upstream)
                with anyio.fail_after(1):
                    await from_upstream.send(error)
                    assert await client_receive.receive() is error
                    with pytest.raises(anyio.WouldBlock):
                        upstream_receive.receive_nowait()
                tg.cancel_scope.cancel()

        anyio.run(scenario)

    @pytest.mark.parametrize("authenticated_before", [False, True])
    @pytest.mark.parametrize("kind", ["auth-code", "elicitation"])
    def test_tools_list_auth_error_returns_authentication_tool_without_login(
        self, monkeypatch, authenticated_before, kind
    ):
        monkeypatch.setattr(mcp_proxy, "get_databricks_token", lambda *a: "tok")
        monkeypatch.setattr(mcp_proxy, "mcp_service_needs_connection_login", lambda *a: True)
        monkeypatch.setattr(
            mcp_proxy,
            "run_connection_login",
            lambda *a, **k: pytest.fail("listing tools must not open a browser"),
        )

        async def scenario():
            from_upstream, upstream_read = anyio.create_memory_object_stream(1)
            to_client, client_receive = anyio.create_memory_object_stream(1)
            to_upstream, upstream_receive = anyio.create_memory_object_stream(1)
            url = f"{WS}/ai-gateway/mcp-services/system.ai.google_calendar"
            bridge = mcp_proxy._McpBridge(mcp_proxy._ConnectionLogin(url, WS, "p"))
            bridge.authentication_completed = authenticated_before
            bridge.pending_requests[1] = self._request()

            async with anyio.create_task_group() as tg:
                tg.start_soon(bridge.pump_upstream, upstream_read, to_client, to_upstream)
                with anyio.fail_after(1):
                    error = self._error() if kind == "auth-code" else self._login_url_failure(kind)
                    await from_upstream.send(error)
                    response = (await client_receive.receive()).message
                    assert isinstance(response, mcp_proxy.JSONRPCResponse)
                    assert response.result["tools"][0]["name"] == "authenticate"
                    assert "same turn" in response.result["tools"][0]["description"]
                    with pytest.raises(anyio.WouldBlock):
                        upstream_receive.receive_nowait()
                tg.cancel_scope.cancel()

        anyio.run(scenario)

    @pytest.mark.parametrize("classification", [False, None])
    def test_discovery_uses_local_login_only_for_supported_elicitation(
        self, monkeypatch, classification
    ):
        monkeypatch.setattr(mcp_proxy, "get_databricks_token", lambda *a: "tok")
        monkeypatch.setattr(
            mcp_proxy, "mcp_service_needs_connection_login", lambda *a: classification
        )

        async def scenario():
            send, receive = anyio.create_memory_object_stream(1)
            bridge = mcp_proxy._McpBridge(mcp_proxy._ConnectionLogin(CONN_URL, WS, "p"))
            error = self._login_url_failure("elicitation", service="github")
            await bridge._send_tool_list_response(error, send)
            assert await receive.receive() is error
            assert not bridge.authentication_tool_available
            assert not bridge.authentication_required

        anyio.run(scenario)

    def test_initialize_advertises_tool_list_changes(self):
        async def scenario():
            from_upstream, upstream_read = anyio.create_memory_object_stream(1)
            to_client, client_receive = anyio.create_memory_object_stream(1)
            to_upstream, _ = anyio.create_memory_object_stream(1)
            bridge = mcp_proxy._McpBridge(mcp_proxy._ConnectionLogin(CONN_URL, WS, "p"))
            bridge.pending_requests[1] = mcp_proxy.SessionMessage(
                mcp_proxy.JSONRPCRequest(
                    jsonrpc="2.0",
                    id=1,
                    method="initialize",
                    params={},
                )
            )
            response = mcp_proxy.SessionMessage(
                mcp_proxy.JSONRPCResponse(
                    jsonrpc="2.0",
                    id=1,
                    result={"capabilities": {"tools": {}}},
                )
            )

            async with anyio.create_task_group() as tg:
                tg.start_soon(bridge.pump_upstream, upstream_read, to_client, to_upstream)
                with anyio.fail_after(1):
                    await from_upstream.send(response)
                    received = (await client_receive.receive()).message
                    assert isinstance(received, mcp_proxy.JSONRPCResponse)
                    assert received.result["capabilities"]["tools"]["listChanged"] is True
                tg.cancel_scope.cancel()

        anyio.run(scenario)

    def test_initialize_without_connection_preserves_capabilities(self):
        async def scenario():
            to_client, client_receive = anyio.create_memory_object_stream(1)
            from_upstream, upstream_read = anyio.create_memory_object_stream(1)
            to_upstream, _ = anyio.create_memory_object_stream(1)
            bridge = mcp_proxy._McpBridge(
                mcp_proxy._ConnectionLogin(CONN_URL, WS, "p", use_pat=True)
            )
            bridge.pending_requests[1] = mcp_proxy.SessionMessage(
                mcp_proxy.JSONRPCRequest(
                    jsonrpc="2.0",
                    id=1,
                    method="initialize",
                    params={},
                )
            )
            response = mcp_proxy.SessionMessage(
                mcp_proxy.JSONRPCResponse(
                    jsonrpc="2.0",
                    id=1,
                    result={"capabilities": {"tools": {}}},
                )
            )

            async with anyio.create_task_group() as tg:
                tg.start_soon(bridge.pump_upstream, upstream_read, to_client, to_upstream)
                with anyio.fail_after(1):
                    await from_upstream.send(response)
                    assert await client_receive.receive() is response
                tg.cancel_scope.cancel()

        anyio.run(scenario)

    def test_authentication_tool_runs_login_after_upstream_auth_error(self, monkeypatch):
        monkeypatch.setattr(mcp_proxy, "get_databricks_token", lambda *a: "tok")
        monkeypatch.setattr(
            mcp_proxy,
            "mcp_service_needs_connection_login",
            lambda *a: False,
        )
        logins = []
        monkeypatch.setattr(
            mcp_proxy,
            "run_connection_login",
            lambda *a, **k: logins.append(1) or (True, "signed in"),
        )

        async def scenario():
            client_send, client_read = anyio.create_memory_object_stream(10)
            to_client, client_receive = anyio.create_memory_object_stream(10)
            from_upstream, upstream_read = anyio.create_memory_object_stream(10)
            to_upstream, upstream_receive = anyio.create_memory_object_stream(10)
            bridge = mcp_proxy._McpBridge(mcp_proxy._ConnectionLogin(CONN_URL, WS, "p"))
            bridge.authentication_required = True

            async with anyio.create_task_group() as tg:
                tg.start_soon(bridge.pump_client, client_read, to_upstream, to_client)
                tg.start_soon(bridge.pump_upstream, upstream_read, to_client, to_upstream)
                with anyio.fail_after(1):
                    await client_send.send(
                        self._tool_call_request(2, mcp_proxy.AUTHENTICATE_TOOL_NAME)
                    )
                    notification = (await client_receive.receive()).message
                    assert isinstance(notification, mcp_proxy.JSONRPCNotification)
                    assert notification.method == "notifications/tools/list_changed"

                    refresh_request = self._request(3)
                    await client_send.send(refresh_request)
                    assert await upstream_receive.receive() is refresh_request
                    refreshed = self._response(3)
                    await from_upstream.send(refreshed)
                    assert await client_receive.receive() is refreshed

                    authentication = (await client_receive.receive()).message
                    assert isinstance(authentication, mcp_proxy.JSONRPCResponse)
                    assert authentication.result["isError"] is False
                tg.cancel_scope.cancel()

        anyio.run(scenario)
        assert logins == [1]

    @pytest.mark.parametrize("refresh_requires_auth", [False, True])
    def test_authentication_tool_refreshes_tools_before_returning(
        self, monkeypatch, refresh_requires_auth
    ):
        monkeypatch.setattr(mcp_proxy, "get_databricks_token", lambda ws, profile: "tok")
        monkeypatch.setattr(mcp_proxy, "mcp_service_needs_connection_login", lambda *a: True)
        logins: list[int] = []
        monkeypatch.setattr(
            mcp_proxy,
            "run_connection_login",
            lambda *a, **k: logins.append(1) or (True, "signed in"),
        )

        async def scenario():
            client_send, client_read = anyio.create_memory_object_stream(10)
            to_client, client_receive = anyio.create_memory_object_stream(10)
            from_upstream, upstream_read = anyio.create_memory_object_stream(10)
            to_upstream, upstream_receive = anyio.create_memory_object_stream(10)
            bridge = mcp_proxy._McpBridge(mcp_proxy._ConnectionLogin(CONN_URL, WS, "p"))
            bridge.authentication_required = True

            async with anyio.create_task_group() as tg:
                tg.start_soon(bridge.pump_client, client_read, to_upstream, to_client)
                tg.start_soon(bridge.pump_upstream, upstream_read, to_client, to_upstream)
                with anyio.fail_after(1):
                    await client_send.send(
                        self._tool_call_request(2, mcp_proxy.AUTHENTICATE_TOOL_NAME)
                    )
                    notification = (await client_receive.receive()).message
                    assert isinstance(notification, mcp_proxy.JSONRPCNotification)
                    assert notification.method == "notifications/tools/list_changed"

                    refresh_request = self._request(3)
                    await client_send.send(refresh_request)
                    assert await upstream_receive.receive() is refresh_request
                    refreshed = mcp_proxy.SessionMessage(
                        mcp_proxy.JSONRPCResponse(
                            jsonrpc="2.0",
                            id=3,
                            result={"tools": [{"name": "search_pull_requests"}]},
                        )
                    )
                    await from_upstream.send(
                        self._error(request_id=3) if refresh_requires_auth else refreshed
                    )
                    received = (await client_receive.receive()).message
                    expected_tools = (
                        [] if refresh_requires_auth else [{"name": "search_pull_requests"}]
                    )
                    assert received.result["tools"] == [
                        *expected_tools,
                        bridge._authentication_tool(),
                    ]

                    authentication = (await client_receive.receive()).message
                    assert isinstance(authentication, mcp_proxy.JSONRPCResponse)
                    assert authentication.id == 2
                    assert authentication.result["isError"] is refresh_requires_auth
                    text = authentication.result["content"][0]["text"]
                    assert (
                        "still requires authentication" if refresh_requires_auth else "same turn"
                    ) in text
                tg.cancel_scope.cancel()

        anyio.run(scenario)
        assert logins == [1]

    def test_authentication_tool_reports_tool_refresh_timeout(self, monkeypatch):
        monkeypatch.setattr(mcp_proxy, "TOOLS_REFRESH_TIMEOUT_SECONDS", 0.01)
        monkeypatch.setattr(
            mcp_proxy,
            "run_connection_login",
            lambda *a, **k: (True, "signed in"),
        )

        async def scenario():
            to_client, client_receive = anyio.create_memory_object_stream(10)
            bridge = mcp_proxy._McpBridge(mcp_proxy._ConnectionLogin(CONN_URL, WS, "p"))
            bridge.authentication_required = True

            await bridge._handle_authentication_tool(2, to_client)
            notification = (await client_receive.receive()).message
            response = (await client_receive.receive()).message

            assert isinstance(notification, mcp_proxy.JSONRPCNotification)
            assert isinstance(response, mcp_proxy.JSONRPCResponse)
            assert "did not refresh before the timeout" in response.result["content"][0]["text"]
            assert "retry authentication" not in response.result["content"][0]["text"]
            assert "start a new session" in response.result["content"][0]["text"]
            assert bridge._tools_refresh_waiters == []

        anyio.run(scenario)

    def test_second_auth_error_is_forwarded_without_another_login(self, monkeypatch):
        monkeypatch.setattr(mcp_proxy, "get_databricks_token", lambda ws, profile: "tok")
        monkeypatch.setattr(mcp_proxy, "mcp_service_needs_connection_login", lambda *a: True)
        logins: list[int] = []
        monkeypatch.setattr(
            mcp_proxy,
            "run_connection_login",
            lambda *a, **k: logins.append(1) or (True, "signed in"),
        )

        async def scenario():
            to_client, client_receive = anyio.create_memory_object_stream(10)
            from_upstream, upstream_read = anyio.create_memory_object_stream(10)
            to_upstream, upstream_receive = anyio.create_memory_object_stream(10)
            bridge = mcp_proxy._McpBridge(mcp_proxy._ConnectionLogin(CONN_URL, WS, "p"))
            request = self._tool_call_request()
            bridge.pending_requests[1] = request

            async with anyio.create_task_group() as tg:
                tg.start_soon(bridge.pump_upstream, upstream_read, to_client, to_upstream)
                with anyio.fail_after(1):
                    await from_upstream.send(self._error())
                    assert await upstream_receive.receive() is request
                    second_error = self._error()
                    await from_upstream.send(second_error)
                    assert await client_receive.receive() is second_error
                tg.cancel_scope.cancel()

        anyio.run(scenario)
        assert logins == [1]

    @pytest.mark.parametrize("with_discovery", [False, True])
    @pytest.mark.parametrize("kind", ["auth-code", "tool-error", "elicitation"])
    def test_automatic_login_does_not_block_other_upstream_messages(
        self, monkeypatch, with_discovery, kind
    ):
        monkeypatch.setattr(mcp_proxy, "get_databricks_token", lambda ws, profile: "tok")
        monkeypatch.setattr(mcp_proxy, "mcp_service_needs_connection_login", lambda *a: True)
        login_started = Event()
        release_login = Event()

        def login(*args, **kwargs):
            login_started.set()
            assert release_login.wait(5)
            return True, "signed in"

        monkeypatch.setattr(mcp_proxy, "run_connection_login", login)

        async def scenario():
            source_send, source_read = anyio.create_memory_object_stream(2)
            dest_send, dest_read = anyio.create_memory_object_stream(2)
            retry_send, retry_read = anyio.create_memory_object_stream(2)
            bridge = mcp_proxy._McpBridge(mcp_proxy._ConnectionLogin(CONN_URL, WS, "p"))
            first_request = self._tool_call_request(1)
            bridge.pending_requests = {
                1: first_request,
                2: self._tool_call_request(2),
                3: self._request(3),
            }
            later_response = self._response(2)

            async with anyio.create_task_group() as tg:
                tg.start_soon(bridge.pump_upstream, source_read, dest_send, retry_send)
                try:
                    with anyio.fail_after(1):
                        failure = (
                            self._error(request_id=1)
                            if kind == "auth-code"
                            else self._login_url_failure(kind, service="github")
                        )
                        await source_send.send(failure)
                        assert await anyio.to_thread.run_sync(login_started.wait, 1)
                        if with_discovery:
                            await source_send.send(self._response(3))
                            received = (await dest_read.receive()).message
                            assert received.result["tools"] == [bridge._authentication_tool()]
                        await source_send.send(later_response)
                        assert await dest_read.receive() is later_response
                        release_login.set()
                        assert await retry_read.receive() is first_request
                finally:
                    release_login.set()
                    tg.cancel_scope.cancel()

        anyio.run(scenario)

    def test_distinct_auth_errors_retry_after_one_login(self, monkeypatch):
        monkeypatch.setattr(mcp_proxy, "get_databricks_token", lambda ws, profile: "tok")
        monkeypatch.setattr(mcp_proxy, "mcp_service_needs_connection_login", lambda *a: True)
        logins: list[int] = []
        monkeypatch.setattr(
            mcp_proxy,
            "run_connection_login",
            lambda *a, **k: logins.append(1) or (True, "signed in"),
        )

        async def scenario():
            to_client, client_receive = anyio.create_memory_object_stream(10)
            from_upstream, upstream_read = anyio.create_memory_object_stream(10)
            to_upstream, upstream_receive = anyio.create_memory_object_stream(10)
            bridge = mcp_proxy._McpBridge(mcp_proxy._ConnectionLogin(CONN_URL, WS, "p"))
            first_request = self._tool_call_request(1)
            second_request = self._tool_call_request(2)
            bridge.pending_requests = {1: first_request, 2: second_request}

            async with anyio.create_task_group() as tg:
                tg.start_soon(bridge.pump_upstream, upstream_read, to_client, to_upstream)
                with anyio.fail_after(1):
                    await from_upstream.send(self._error(request_id=1))
                    assert await upstream_receive.receive() is first_request
                    await from_upstream.send(self._error(request_id=2))
                    assert await upstream_receive.receive() is second_request
                    with pytest.raises(anyio.WouldBlock):
                        client_receive.receive_nowait()
                tg.cancel_scope.cancel()

        anyio.run(scenario)
        assert logins == [1]

    @pytest.mark.parametrize(
        ("url", "use_pat"),
        [
            (URL, False),
            (CONN_URL, True),
        ],
    )
    def test_non_connection_and_pat_auth_errors_pass_through_without_login(
        self, monkeypatch, url, use_pat
    ):
        logins: list[int] = []
        monkeypatch.setattr(
            mcp_proxy,
            "run_connection_login",
            lambda *a, **k: logins.append(1) or (True, "signed in"),
        )

        async def scenario():
            source_send, source_read = anyio.create_memory_object_stream(1)
            dest_send, dest_read = anyio.create_memory_object_stream(1)
            retry_send, retry_read = anyio.create_memory_object_stream(1)
            bridge = mcp_proxy._McpBridge(mcp_proxy._ConnectionLogin(url, WS, "p", use_pat=use_pat))
            bridge.pending_requests[1] = self._request()
            error = self._error()
            await source_send.send(error)
            await source_send.aclose()

            with pytest.raises(mcp_proxy.ProxyTransportError, match="closed unexpectedly"):
                await bridge.pump_upstream(source_read, dest_send, retry_send)
            assert await dest_read.receive() is error
            with pytest.raises(anyio.WouldBlock):
                retry_read.receive_nowait()

        anyio.run(scenario)
        assert logins == []

    @pytest.mark.parametrize("code", [-32601, -32002, -32042])
    def test_other_jsonrpc_error_passes_through(self, monkeypatch, code):
        monkeypatch.setattr(
            mcp_proxy,
            "run_connection_login",
            lambda *a, **k: pytest.fail("login must not run"),
        )

        async def scenario():
            source_send, source_read = anyio.create_memory_object_stream(1)
            dest_send, dest_read = anyio.create_memory_object_stream(1)
            retry_send, _ = anyio.create_memory_object_stream(1)
            bridge = mcp_proxy._McpBridge(mcp_proxy._ConnectionLogin(CONN_URL, WS, "p"))
            bridge.pending_requests[1] = self._request()
            error = self._error(code=code)
            await source_send.send(error)
            await source_send.aclose()

            with pytest.raises(mcp_proxy.ProxyTransportError, match="closed unexpectedly"):
                await bridge.pump_upstream(source_read, dest_send, retry_send)
            assert await dest_read.receive() is error

        anyio.run(scenario)

    def test_automatic_login_failure_forwards_error_and_keeps_bridge_alive(self, monkeypatch):
        monkeypatch.setattr(mcp_proxy, "get_databricks_token", lambda ws, profile: "tok")
        monkeypatch.setattr(mcp_proxy, "mcp_service_needs_connection_login", lambda *a: True)
        monkeypatch.setattr(
            mcp_proxy,
            "run_connection_login",
            lambda *a, **k: (False, "user cancelled"),
        )

        async def scenario():
            source_send, source_read = anyio.create_memory_object_stream(2)
            dest_send, dest_read = anyio.create_memory_object_stream(2)
            retry_send, _ = anyio.create_memory_object_stream(2)
            bridge = mcp_proxy._McpBridge(mcp_proxy._ConnectionLogin(CONN_URL, WS, "p"))
            bridge.pending_requests = {
                1: self._tool_call_request(1),
                2: self._tool_call_request(2),
            }
            auth_error = self._error(request_id=1)
            later_response = self._response(2)

            async with anyio.create_task_group() as tg:
                tg.start_soon(bridge.pump_upstream, source_read, dest_send, retry_send)
                with anyio.fail_after(1):
                    await source_send.send(auth_error)
                    assert await dest_read.receive() is auth_error
                    await source_send.send(later_response)
                    assert await dest_read.receive() is later_response
                tg.cancel_scope.cancel()

        anyio.run(scenario)

    def test_notifications_are_forwarded_without_becoming_pending_requests(self):
        from mcp.types import JSONRPCNotification

        async def scenario():
            source_send, source_read = anyio.create_memory_object_stream(1)
            dest_send, dest_read = anyio.create_memory_object_stream(1)
            bridge = mcp_proxy._McpBridge(mcp_proxy._ConnectionLogin(CONN_URL, WS, "p"))
            notification = mcp_proxy.SessionMessage(
                JSONRPCNotification(jsonrpc="2.0", method="notifications/initialized")
            )
            await source_send.send(notification)
            await source_send.aclose()

            await bridge.pump_client(source_read, dest_send)
            assert await dest_read.receive() is notification
            assert bridge.pending_requests == {}

        anyio.run(scenario)


def test_run_uses_mcp_http_defaults(monkeypatch):
    httpx_module = mcp_proxy._httpx()
    captured: dict = {}

    class CapturingClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class StopBridge(Exception):
        pass

    @asynccontextmanager
    async def stop_bridge(*args, **kwargs):
        raise StopBridge
        yield

    monkeypatch.setattr(httpx_module, "AsyncClient", CapturingClient)
    monkeypatch.setattr(mcp_proxy, "_build_token_auth", lambda *args, **kwargs: object())
    monkeypatch.setattr(mcp_proxy, "streamable_http_client", stop_bridge)

    with pytest.raises(StopBridge):
        anyio.run(mcp_proxy._run, URL, WS, None)

    timeout = captured["timeout"]
    assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (30.0, 300.0, 30.0, 30.0)


class TestServe:
    def test_runs_the_bridge_with_parsed_args(self, monkeypatch):
        captured: dict = {}

        def fake_run(func, *args):
            captured["func"] = func
            captured["args"] = args

        monkeypatch.setattr(mcp_proxy, "_preflight_token", lambda ws, profile: None)
        monkeypatch.setattr(mcp_proxy.anyio, "run", fake_run)

        mcp_proxy.serve(URL, WS, "uc-dogfood")

        assert captured["func"] is mcp_proxy._run
        # PAT is resolved inside the token mint, so _run takes no use_pat arg.
        assert captured["args"] == (URL, WS, "uc-dogfood", False)

    def test_defaults_profile_none(self, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(mcp_proxy, "_preflight_token", lambda ws, profile: None)
        monkeypatch.setattr(mcp_proxy.anyio, "run", lambda func, *args: captured.update(args=args))

        mcp_proxy.serve(URL, WS)

        assert captured["args"] == (URL, WS, None, False)

    def test_use_pat_exports_the_bearer_before_serving(self, monkeypatch):
        # PAT auth: the profile's static token must be exported (ensure_pat_bearer)
        # before the token preflight, so the per-request mint's DATABRICKS_BEARER
        # short-circuit returns it. `databricks auth token` can't read a PAT itself.
        order: list[str] = []
        monkeypatch.setattr(
            mcp_proxy, "ensure_pat_bearer", lambda profile: order.append(f"pat:{profile}") or True
        )
        monkeypatch.setattr(
            mcp_proxy, "_preflight_token", lambda ws, profile: order.append("preflight")
        )
        monkeypatch.setattr(mcp_proxy.anyio, "run", lambda func, *args: order.append("bridge"))

        mcp_proxy.serve(URL, WS, "patprof", use_pat=True)

        assert order == ["pat:patprof", "preflight", "bridge"]

    def test_use_pat_without_a_resolvable_pat_exits_before_serving(self, monkeypatch, capsys):
        started: list[str] = []
        monkeypatch.setattr(mcp_proxy, "ensure_pat_bearer", lambda profile: False)
        monkeypatch.setattr(mcp_proxy, "_preflight_token", lambda ws, profile: None)
        monkeypatch.setattr(mcp_proxy.anyio, "run", lambda func, *args: started.append("bridge"))

        with pytest.raises(SystemExit) as excinfo:
            mcp_proxy.serve(URL, WS, "nopat", use_pat=True)

        assert excinfo.value.code == mcp_proxy.AUTH_FAILURE_EXIT_CODE
        assert started == []  # never opened the bridge
        assert "no personal access token" in capsys.readouterr().err

    def test_oauth_path_never_touches_pat(self, monkeypatch):
        # Without use_pat, ensure_pat_bearer must not be consulted at all.
        called: list[str] = []
        monkeypatch.setattr(
            mcp_proxy, "ensure_pat_bearer", lambda profile: called.append(profile) or True
        )
        monkeypatch.setattr(mcp_proxy, "_preflight_token", lambda ws, profile: None)
        monkeypatch.setattr(mcp_proxy.anyio, "run", lambda func, *args: None)

        mcp_proxy.serve(URL, WS, "oauthprof", use_pat=False)

        assert called == []

    def test_preflights_auth_before_opening_the_bridge(self, monkeypatch):
        # Order matters: a dead profile must be caught before the stdio bridge
        # starts, so the failure is a fast exit rather than a stalled session.
        order: list[str] = []
        monkeypatch.setattr(
            mcp_proxy, "_preflight_token", lambda ws, profile: order.append("preflight")
        )
        monkeypatch.setattr(mcp_proxy.anyio, "run", lambda func, *args: order.append("bridge"))

        mcp_proxy.serve(URL, WS, "p")

        assert order == ["preflight", "bridge"]

    def test_dead_auth_exits_fast_without_starting_the_bridge(self, monkeypatch, capsys):
        # The regression this fix targets: previously the token failure surfaced
        # from inside the transport and the proxy hung until the MCP client's
        # startup timeout (~30s) with no explanation.
        started: list[str] = []

        def dead_auth(ws, profile):
            raise RuntimeError("no access token for " + ws + "; run `databricks auth login`")

        monkeypatch.setattr(mcp_proxy, "_preflight_token", dead_auth)
        monkeypatch.setattr(mcp_proxy.anyio, "run", lambda func, *args: started.append("bridge"))

        with pytest.raises(SystemExit) as excinfo:
            mcp_proxy.serve(URL, WS, "p")

        assert excinfo.value.code == mcp_proxy.AUTH_FAILURE_EXIT_CODE
        assert started == []  # the bridge never opened
        # Diagnostics go to stderr; stdout is the MCP wire and must stay clean.
        captured = capsys.readouterr()
        assert "databricks auth login" in captured.err
        assert captured.out == ""

    def test_auth_expiring_mid_session_exits_with_the_actionable_message(self, monkeypatch, capsys):
        # A ProxyAuthError raised once the bridge is running arrives wrapped in an
        # anyio ExceptionGroup; it must still be reported, not surface as a crash.
        def raise_group(func, *args):
            raise BaseExceptionGroup(
                "transport",
                [mcp_proxy.ProxyAuthError("token expired; run `databricks auth login`")],
            )

        monkeypatch.setattr(mcp_proxy, "_preflight_token", lambda ws, profile: None)
        monkeypatch.setattr(mcp_proxy.anyio, "run", raise_group)

        with pytest.raises(SystemExit) as excinfo:
            mcp_proxy.serve(URL, WS, "p")

        assert excinfo.value.code == mcp_proxy.AUTH_FAILURE_EXIT_CODE
        assert "token expired" in capsys.readouterr().err

    def test_transport_failure_exits_with_a_one_line_message(self, monkeypatch, capsys):
        def raise_group(func, *args):
            raise BaseExceptionGroup(
                "transport",
                [mcp_proxy.ProxyTransportError("upstream MCP transport closed unexpectedly")],
            )

        monkeypatch.setattr(mcp_proxy, "_preflight_token", lambda ws, profile: None)
        monkeypatch.setattr(mcp_proxy.anyio, "run", raise_group)

        with pytest.raises(SystemExit) as excinfo:
            mcp_proxy.serve(URL, WS, "p")

        assert excinfo.value.code == mcp_proxy.AUTH_FAILURE_EXIT_CODE
        captured = capsys.readouterr()
        assert captured.err == "ug mcp-proxy: upstream MCP transport closed unexpectedly\n"
        assert captured.out == ""

    def test_non_auth_failures_still_propagate(self, monkeypatch):
        # Only expected proxy failures are converted to a clean exit; programming
        # bugs must keep their traceback so they stay debuggable.
        def raise_other(func, *args):
            raise ValueError("some transport bug")

        monkeypatch.setattr(mcp_proxy, "_preflight_token", lambda ws, profile: None)
        monkeypatch.setattr(mcp_proxy.anyio, "run", raise_other)

        with pytest.raises(ValueError, match="some transport bug"):
            mcp_proxy.serve(URL, WS, "p")


class TestPreflightToken:
    def test_passes_through_when_a_token_is_available(self, monkeypatch):
        monkeypatch.setattr(mcp_proxy, "get_databricks_token", lambda ws, profile: "tok")
        mcp_proxy._preflight_token(WS, "p")  # no exception

    def test_surfaces_the_cli_error_message(self, monkeypatch):
        def boom(ws, profile):
            raise RuntimeError("profile is stale; run `databricks auth logout`")

        monkeypatch.setattr(mcp_proxy, "get_databricks_token", boom)

        with pytest.raises(RuntimeError, match="databricks auth logout"):
            mcp_proxy._preflight_token(WS, "p")

    def test_checks_the_same_workspace_and_profile_the_bridge_will_use(self, monkeypatch):
        # The preflight must validate the exact credentials the request-time auth
        # hook uses, or it could pass while the bridge still fails.
        calls: list[tuple[str, str | None]] = []
        monkeypatch.setattr(
            mcp_proxy,
            "get_databricks_token",
            lambda ws, profile: calls.append((ws, profile)) or "tok",
        )

        mcp_proxy._preflight_token(WS, "myprofile")

        assert calls == [(WS, "myprofile")]
