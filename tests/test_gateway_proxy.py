"""Tests for the relayed refresh proxy header handling."""

from __future__ import annotations

import base64
import io
import json
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock
from urllib.parse import quote

import httpx
import pytest

from ucode import gateway_proxy


def _make_jwt(exp: float | None) -> str:
    """A minimal JWT-shaped token whose payload carries `exp` (or none)."""
    claims = {"exp": exp} if exp is not None else {"sub": "x"}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"header.{payload}.sig"


class _FakeHandler:
    """Minimal stand-in exposing a `.headers` mapping like BaseHTTPRequestHandler."""

    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


class TestForwardedRequestHeaders:
    def test_injects_swap_header_with_bearer(self):
        handler = _FakeHandler({"Authorization": "Bearer anthropic-oauth"})
        out = gateway_proxy.forwarded_request_headers(handler, "dbx-token")
        assert out["X-Databricks-AI-Gateway-Token"] == "Bearer dbx-token"

    def test_passes_authorization_through_untouched(self):
        # The caller's Anthropic OAuth must survive verbatim — the proxy never
        # reads or rewrites it.
        handler = _FakeHandler({"Authorization": "Bearer anthropic-oauth"})
        out = gateway_proxy.forwarded_request_headers(handler, "dbx-token")
        assert out["Authorization"] == "Bearer anthropic-oauth"

    def test_requests_identity_encoding_unless_client_asks(self):
        # Codex sends no Accept-Encoding and can't decode gzip; httpx would otherwise ask for it.
        out = gateway_proxy.forwarded_request_headers(_FakeHandler({}), "t")
        assert out["Accept-Encoding"] == "identity"
        handler = _FakeHandler({"accept-encoding": "gzip, br"})
        out = gateway_proxy.forwarded_request_headers(handler, "t")
        assert out["accept-encoding"] == "gzip, br"
        assert "Accept-Encoding" not in out

    def test_overwrites_client_supplied_swap_header(self):
        # A stale settings.json value must not survive; the proxy replaces it.
        handler = _FakeHandler({"X-Databricks-AI-Gateway-Token": "Bearer stale"})
        out = gateway_proxy.forwarded_request_headers(handler, "fresh")
        assert out["X-Databricks-AI-Gateway-Token"] == "Bearer fresh"

    def test_overwrites_authorization_header(self):
        handler = _FakeHandler({"Authorization": "Bearer stale"})
        out = gateway_proxy.forwarded_request_headers(
            handler, "fresh", gateway_proxy.AUTHORIZATION_HEADER
        )
        assert out["Authorization"] == "Bearer fresh"

    def test_strips_hop_by_hop_headers(self):
        handler = _FakeHandler(
            {"Host": "localhost:9", "Content-Length": "5", "Connection": "keep-alive"}
        )
        out = gateway_proxy.forwarded_request_headers(handler, "t")
        assert "Host" not in out
        assert "Content-Length" not in out
        assert "Connection" not in out

    def test_extra_strip_removes_named_headers(self):
        handler = _FakeHandler({"Databricks-Model-Provider-Service": "cat.s.mps", "Keep": "me"})
        out = gateway_proxy.forwarded_request_headers(
            handler, "t", extra_strip=frozenset({"databricks-model-provider-service"})
        )
        assert "Databricks-Model-Provider-Service" not in out
        assert out["Keep"] == "me"

    def test_databricks_route_swaps_authorization_and_drops_relay_headers(self):
        # OSS path: the Databricks token replaces the caller's OAuth in Authorization, and
        # the swap + MPS headers are dropped so the gateway serves the model directly.
        handler = _FakeHandler(
            {
                "Authorization": "Bearer anthropic-oauth",
                "X-Databricks-AI-Gateway-Token": "Bearer stale-swap",
                "Databricks-Model-Provider-Service": "cat.s.relayed_mps",
            }
        )
        out = gateway_proxy.forwarded_request_headers(
            handler,
            "dbx-token",
            gateway_proxy.AUTHORIZATION_HEADER,
            extra_strip=gateway_proxy._DATABRICKS_ROUTE_STRIP,
        )
        assert out["Authorization"] == "Bearer dbx-token"
        assert "X-Databricks-AI-Gateway-Token" not in out
        assert "Databricks-Model-Provider-Service" not in out


class TestIsDatabricksRoutedModel:
    def test_namespace_qualified_ids_route_to_databricks(self):
        for model in (
            "system.ai.claude-opus-4-8",
            "system.ai.gpt-oss-120b",
            "my_catalog.my_schema.my_model",
            "databricks-meta-llama-3-3-70b-instruct",
        ):
            assert gateway_proxy.is_databricks_routed_model(model), model

    def test_bare_anthropic_ids_relay(self):
        for model in ("claude-opus-4-1", "claude-sonnet-4-5", "claude-3-7-sonnet-20250219"):
            assert not gateway_proxy.is_databricks_routed_model(model), model

    def test_missing_model_relays(self):
        assert not gateway_proxy.is_databricks_routed_model(None)
        assert not gateway_proxy.is_databricks_routed_model("")


class TestRequestModel:
    def test_extracts_model_from_json_body(self):
        assert gateway_proxy._request_model(b'{"model": "system.ai.x", "n": 1}') == "system.ai.x"

    def test_none_for_missing_body(self):
        assert gateway_proxy._request_model(None) is None

    def test_none_for_invalid_json(self):
        assert gateway_proxy._request_model(b"not json") is None

    def test_none_for_non_object_or_non_string_model(self):
        assert gateway_proxy._request_model(b"[1, 2]") is None
        assert gateway_proxy._request_model(b'{"model": 5}') is None


class _FakeResponse:
    """Stand-in for httpx.Response exposing only what `_relay_response` reads."""

    def __init__(self, status_code: int, headers: dict[str, str], chunks):
        self.status_code = status_code
        self.headers = headers
        self._chunks = chunks
        self.chunk_sizes = []

    def iter_raw(self, chunk_size=None):
        self.chunk_sizes.append(chunk_size)
        yield from self._chunks


class _BrokenPipeWriter(io.RawIOBase):
    """A wfile stand-in that raises BrokenPipeError on write, mimicking a client
    (Claude Code) that closed the connection mid-response."""

    def write(self, _data):  # type: ignore[override]
        raise BrokenPipeError(32, "Broken pipe")


def _relay_handler(wfile) -> gateway_proxy._ProxyHandler:
    # Bypass BaseHTTPRequestHandler.__init__ (which would service a socket);
    # we only exercise _relay_response's write path. Set the few attributes the
    # send_response/send_header machinery reads (normally populated by __init__).
    handler = object.__new__(gateway_proxy._ProxyHandler)
    handler.wfile = wfile
    handler.request_version = "HTTP/1.1"
    handler.requestline = "POST /v1/messages HTTP/1.1"
    handler.command = "POST"
    handler._headers_buffer = []
    return handler


class TestRelayResponseClientDisconnect:
    def test_relay_swallows_broken_pipe_on_headers(self):
        # Client gone before headers flush: end_headers write raises BrokenPipe.
        handler = _relay_handler(_BrokenPipeWriter())
        resp = _FakeResponse(200, {}, [b'{"ok":true}'])
        # Must not raise — a dead client is a routine teardown, not an error.
        handler._relay_response(resp)

    def test_relay_swallows_connection_reset_mid_stream(self):
        # Headers flush ok, then the client resets while streaming body chunks.
        writes: list[bytes] = []

        class _ResetAfterHeaders(io.RawIOBase):
            def write(self, data):  # type: ignore[override]
                writes.append(bytes(data))
                if b"chunk" in bytes(data):
                    raise ConnectionResetError(54, "Connection reset by peer")
                return len(data)

            def flush(self):
                return None

        handler = _relay_handler(_ResetAfterHeaders())
        resp = _FakeResponse(200, {}, [b"chunk-of-sse-data"])
        handler._relay_response(resp)

    def test_relay_swallows_upstream_error_mid_stream(self):
        # Upstream drops mid-body after headers are already sent — we can't signal
        # a fresh error, so stop quietly rather than corrupt the response framing.
        class _Ok(io.RawIOBase):
            def write(self, data):  # type: ignore[override]
                return len(data)

            def flush(self):
                return None

        def _chunks():
            yield b"partial"
            raise httpx.ReadError("upstream dropped")

        handler = _relay_handler(_Ok())
        resp = _FakeResponse(200, {}, _chunks())
        handler._relay_response(resp)  # must not raise

    def test_relay_forwards_status_and_skips_hop_by_hop_headers(self):
        # A non-200 status (e.g. 429 rate limit) and content headers are relayed;
        # hop-by-hop framing headers are dropped.
        chunks_written: list[bytes] = []

        class _Collect(io.RawIOBase):
            def write(self, data):  # type: ignore[override]
                chunks_written.append(bytes(data))
                return len(data)

            def flush(self):
                return None

        handler = _relay_handler(_Collect())
        resp = _FakeResponse(
            429,
            {"Content-Type": "application/json", "Transfer-Encoding": "chunked"},
            [b'{"type":"error"}'],
        )
        handler._relay_response(resp)
        blob = b"".join(chunks_written)
        assert b"429" in blob
        assert b"Content-Type: application/json" in blob
        assert b"Transfer-Encoding" not in blob  # hop-by-hop, stripped
        assert resp.chunk_sizes == [None]  # relay each upstream SSE/network chunk immediately

    def test_diagnostics_identify_upstream_mid_stream_drop(self, monkeypatch, capsys):
        monkeypatch.setenv(gateway_proxy._DIAGNOSTICS_ENV, "1")

        class _Ok(io.RawIOBase):
            def write(self, data):  # type: ignore[override]
                return len(data)

            def flush(self):
                return None

        def _chunks():
            yield b"partial"
            raise httpx.ReadError("upstream dropped")

        handler = _relay_handler(_Ok())
        handler._relay_response(
            _FakeResponse(200, {}, _chunks()),
            diagnostic_id="local-id",
            started=time.monotonic(),
        )

        line = capsys.readouterr().err.strip()
        assert line.startswith("[ucode-relay] ")
        event = json.loads(line.removeprefix("[ucode-relay] "))
        assert event["event"] == "upstream_stream_error"
        assert event["request_id"] == "local-id"
        assert event["error_type"] == "ReadError"
        assert event["bytes"] == len(b"partial")
        assert "upstream dropped" not in line

    def test_diagnostics_are_silent_by_default(self, monkeypatch, capsys):
        monkeypatch.delenv(gateway_proxy._DIAGNOSTICS_ENV, raising=False)
        handler = _relay_handler(_Collect())
        handler._relay_response(_FakeResponse(200, {}, [b"ok"]))
        assert capsys.readouterr().err == ""

    @pytest.mark.parametrize(
        ("interactive", "log_directory_available"), [(True, True), (True, False), (False, True)]
    )
    def test_interactive_proxy_logs_preserve_terminal_output(
        self, monkeypatch, capsys, tmp_path, interactive, log_directory_available
    ):
        monkeypatch.setenv(gateway_proxy._DIAGNOSTICS_ENV, "1")
        monkeypatch.setattr(gateway_proxy.sys.stderr, "isatty", lambda: interactive)
        monkeypatch.setattr(gateway_proxy, "APP_DIR", tmp_path)
        if not log_directory_available:
            (tmp_path / "logs").write_text("existing file")

        handler = _relay_handler(_Collect())
        handler._relay_response(
            _FakeResponse(200, {}, [b"ok"]), diagnostic_id="local-id", started=time.monotonic()
        )
        proxy, cache, client = gateway_proxy.start_claude_language_proxy(
            "http://127.0.0.1:1", lambda _force: "test-token", session_secret="test-secret"
        )
        try:
            cache.report_refresh_failure(
                RuntimeError("Command: broker --token synthetic-credential; Stderr: private-error")
            )
        finally:
            proxy.server_close()
            cache.stop()
            client.close()

        output = capsys.readouterr()
        path = tmp_path / "logs" / "gateway-proxy.log"
        assert output.out == ""
        if interactive:
            assert output.err == ""
        else:
            assert not path.exists()
        if log_directory_available:
            lines = (path.read_text() if interactive else output.err).splitlines()
            event = json.loads(lines[0].removeprefix("[ucode-relay] "))
            assert event["event"] == "response_complete"
            assert event["request_id"] == "local-id"
            assert event["bytes"] == 2
            assert "token refresh failed." in lines[1]
            assert "Refresh your workspace sign-in and restart ug claude." in lines[1]
            assert "synthetic-credential" not in "\n".join(lines)
            assert "private-error" not in "\n".join(lines)
            assert "Command:" not in "\n".join(lines)
            if interactive and os.name != "nt":
                assert path.stat().st_mode & 0o777 == 0o600

    def test_default_refresh_failure_keeps_interactive_sign_in_hint(self, monkeypatch, capsys):
        monkeypatch.setattr(gateway_proxy.sys.stderr, "isatty", lambda: True)
        gateway_proxy.log_token_refresh_failure(RuntimeError("refresh unavailable"))
        output = capsys.readouterr()
        assert output.out == ""
        assert "token refresh failed: refresh unavailable" in output.err
        assert "databricks auth login" in output.err


class TestJwtExp:
    def test_extracts_exp(self):
        assert gateway_proxy._jwt_exp(_make_jwt(1234567890.0)) == 1234567890.0

    def test_none_on_missing_exp(self):
        assert gateway_proxy._jwt_exp(_make_jwt(None)) is None

    def test_none_on_garbage(self):
        assert gateway_proxy._jwt_exp("not-a-jwt") is None


def _fake_token_provider(exp_offsets, delay=0.0):
    """Build a ``token_provider(force)`` that hands out JWTs whose exp is now+offset,
    one per successive mint (last offset repeats), recording each force flag. Set
    ``state['raise'] = True`` to make subsequent mints fail."""
    state = {"i": 0, "forces": [], "raise": False}

    def provider(force_refresh=False):
        if delay:
            time.sleep(delay)
        if state["raise"]:
            raise RuntimeError("mint failed")
        off = exp_offsets[min(state["i"], len(exp_offsets) - 1)]
        state["i"] += 1
        state["forces"].append(force_refresh)
        return _make_jwt(time.time() + off)

    return state, provider


class TestTokenCache:
    def test_initial_mint_preserves_default_nonforce_refresh(self):
        state, provider = _fake_token_provider([5000])
        gateway_proxy.TokenCache(provider)
        assert state["forces"] == [False]

    def test_fresh_token_is_not_refreshed(self):
        state, provider = _fake_token_provider([5000])
        cache = gateway_proxy.TokenCache(provider)
        _ = cache.token
        _ = cache.token
        assert state["forces"] == [False]  # no extra mint while fresh

    def test_near_expiry_preserves_default_nonforce_refresh(self):
        state, provider = _fake_token_provider([100, 5000])
        cache = gateway_proxy.TokenCache(provider)
        _ = cache.token
        assert state["forces"] == [False, False]
        _ = cache.token  # now fresh again
        assert state["forces"] == [False, False]

    def test_near_expiry_can_force_refresh(self):
        state, provider = _fake_token_provider([100, 5000])
        cache = gateway_proxy.TokenCache(provider, force_refresh_near_expiry=True)
        _ = cache.token
        assert state["forces"] == [True, True]

    def test_refresh_is_single_flighted(self):
        # A burst of concurrent requests at the expiry boundary must trigger ONE
        # refresh, not a thundering herd on the shared token cache.
        state, provider = _fake_token_provider([100, 5000], delay=0.05)
        cache = gateway_proxy.TokenCache(provider, force_refresh_near_expiry=True)
        threads = [threading.Thread(target=lambda: cache.token) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 1 forced init + exactly 1 forced refresh shared by all 10 readers.
        assert state["forces"] == [True, True]

    def test_ensure_fresh_keeps_token_when_refresh_fails(self):
        state, provider = _fake_token_provider([5000])
        cache = gateway_proxy.TokenCache(provider)
        good = cache.token

        state["raise"] = True  # subsequent mints fail
        # Force staleness so _ensure_fresh attempts a refresh, which now fails.
        cache._expiry = time.time()
        assert cache.token == good  # last good token retained, no exception

    def test_refresher_loop_survives_unexpected_error(self, monkeypatch):
        _state, provider = _fake_token_provider([5000])
        cache = gateway_proxy.TokenCache(provider)
        monkeypatch.setattr(gateway_proxy, "_REFRESHER_POLL_S", 0.01)
        ticks = []

        def boom():
            ticks.append(1)
            cache.stop()  # let the loop exit after this tick
            raise ValueError("unexpected, non-RuntimeError")

        monkeypatch.setattr(cache, "_ensure_fresh", boom)
        cache.run_refresher()  # must RETURN, not propagate — else the thread dies
        assert ticks == [1]

    def test_failed_rejection_refresh_remains_retryable(self):
        state, provider = _fake_token_provider([5000])
        cache = gateway_proxy.TokenCache(provider)
        original = cache.token_snapshot()
        state["raise"] = True
        with pytest.raises(RuntimeError, match="mint failed"):
            cache.refresh(rejected_generation=original[1])
        assert cache.token_snapshot() == original
        state["raise"] = False
        cache.refresh(rejected_generation=original[1])
        assert cache.token_snapshot()[1] == original[1] + 1
        assert state["forces"] == [False, True]


class _FakeResp:
    def __init__(self, status: int, body: bytes = b"", headers: dict | None = None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.read_called = False

    def read(self):
        self.read_called = True
        return self._body

    def iter_raw(self, _n=None):
        yield self._body

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class _FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.sent_tokens: list[str | None] = []
        self.sent_headers: list[dict] = []

    def stream(self, _method, _url, headers, content):
        self.sent_tokens.append(headers.get(gateway_proxy.AI_GATEWAY_TOKEN_HEADER))
        self.sent_headers.append(headers)
        return self._responses.pop(0)


class _FakeCache:
    def __init__(self):
        self._t = "Bearer-tok1"
        self.refreshed = 0

    @property
    def token(self):
        return self._t.replace("Bearer-", "")

    def token_snapshot(self):
        return self.token, self.refreshed

    def refresh(self, *, rejected_generation=None):
        self.refreshed += 1
        self._t = "Bearer-tok2"


class _RefreshFailsCache:
    """A cache whose forced refresh raises, mimicking a dead Databricks OAuth
    session that can't be re-minted non-interactively."""

    def __init__(self):
        self.refreshed = 0

    @property
    def token(self):
        return "tok1"

    def token_snapshot(self):
        return self.token, self.refreshed

    def refresh(self, *, rejected_generation=None):
        self.refreshed += 1
        raise RuntimeError("mint failed")


def _handle_handler(client, cache, wfile) -> gateway_proxy._ProxyHandler:
    h = object.__new__(gateway_proxy._ProxyHandler)
    h.client = client
    h.cache = cache
    h.headers = {}
    h.rfile = io.BytesIO(b"")
    h.path = "/v1/messages"
    h.command = "POST"
    h.wfile = wfile
    h.request_version = "HTTP/1.1"
    h.requestline = "POST /v1/messages HTTP/1.1"
    h._headers_buffer = []
    return h


class _Collect(io.RawIOBase):
    def __init__(self):
        self.data = bytearray()

    def write(self, b):  # type: ignore[override]
        self.data += bytes(b)
        return len(b)

    def flush(self):
        return None


class TestRetryOn401:
    def test_diagnostics_separate_token_preparation_from_upstream_attempts(
        self, monkeypatch, capsys
    ):
        monkeypatch.setenv(gateway_proxy._DIAGNOSTICS_ENV, "1")
        elapsed = [0.0]
        monkeypatch.setattr(gateway_proxy.time, "monotonic", lambda: elapsed[0])

        class TimedCache(_FakeCache):
            def token_snapshot(self):
                elapsed[0] = 1.0
                return super().token_snapshot()

            def refresh(self, **kwargs):
                elapsed[0] = 3.0
                return super().refresh(**kwargs)

        class TimedClient(_FakeClient):
            def stream(self, *args, **kwargs):
                elapsed[0] += 1.0
                return super().stream(*args, **kwargs)

        class TimedHeaders(dict):
            def items(self):
                elapsed[0] += 0.25
                return super().items()

        client = TimedClient([_FakeResp(401), _FakeResp(200, b"private-response")])
        output = _Collect()
        handler = _handle_handler(client, TimedCache(), output)
        handler.headers = TimedHeaders(
            {"Authorization": "Bearer private-token", "Content-Length": "15"}
        )
        handler.rfile = io.BytesIO(b"private-request")
        started_ms = int(time.time() * 1000)
        handler._handle()

        logged = capsys.readouterr().err
        events = [json.loads(line.removeprefix("[ucode-relay] ")) for line in logged.splitlines()]
        assert [event["event"] for event in events] == [
            "request_start",
            "upstream_request_start",
            "upstream_headers",
            "upstream_request_start",
            "upstream_headers",
            "response_complete",
        ]
        assert [(event["attempt"], event["elapsed_ms"]) for event in events[1:-1]] == [
            (1, 1250),
            (1, 2250),
            (2, 3250),
            (2, 4250),
        ]
        assert len({event["request_id"] for event in events}) == 1
        assert all(
            started_ms <= event["timestamp_ms"] <= int(time.time() * 1000) for event in events
        )
        assert "private-" not in logged
        assert bytes(output.data).endswith(b"private-response")

    @pytest.mark.parametrize("status", [401, 403])
    @pytest.mark.parametrize("same_token", [False, True])
    def test_concurrent_rejections_share_one_refresh(self, status, same_token):
        forces = []

        def provider(force):
            forces.append(force)
            return "tok1" if same_token or not force else "tok2"

        cache = gateway_proxy.TokenCache(provider)
        barrier = threading.Barrier(8, timeout=10)

        class RejectedClient(_FakeClient):
            def stream(self, *args, **kwargs):
                response = super().stream(*args, **kwargs)
                if len(self.sent_tokens) == 1:
                    barrier.wait()
                return response

        clients = [RejectedClient([_FakeResp(status), _FakeResp(200, b"ok")]) for _ in range(8)]
        outputs = [_Collect() for _ in clients]
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [
                pool.submit(_handle_handler(client, cache, output)._handle)
                for client, output in zip(clients, outputs, strict=True)
            ]
            for future in futures:
                future.result(timeout=10)
        assert forces == [False, True]
        for client, output in zip(clients, outputs, strict=True):
            assert client.sent_tokens == [
                "Bearer tok1",
                "Bearer tok1" if same_token else "Bearer tok2",
            ]
            assert bytes(output.data).endswith(b"ok")

    def test_delayed_rejection_uses_already_refreshed_token(self):
        forces = []

        def provider(force):
            forces.append(force)
            return "tok2" if force else "tok1"

        cache = gateway_proxy.TokenCache(provider)

        class DelayedRejection(_FakeClient):
            def stream(self, *args, **kwargs):
                response = super().stream(*args, **kwargs)
                if len(self.sent_tokens) == 1:
                    cache.refresh()
                return response

        client = DelayedRejection([_FakeResp(401), _FakeResp(200, b"ok")])
        output = _Collect()
        _handle_handler(client, cache, output)._handle()
        assert forces == [False, True]
        assert client.sent_tokens == ["Bearer tok1", "Bearer tok2"]
        assert bytes(output.data).endswith(b"ok")

    @pytest.mark.parametrize("background_rotates", [False, True])
    def test_nonforced_lookup_only_supersedes_a_rejection_if_token_changes(
        self, background_rotates
    ):
        forces = []

        def provider(force):
            forces.append(force)
            return "tok2" if force or (background_rotates and len(forces) > 1) else "tok1"

        cache = gateway_proxy.TokenCache(provider)

        class BackgroundRefreshClient(_FakeClient):
            def stream(self, *args, **kwargs):
                response = super().stream(*args, **kwargs)
                if len(self.sent_tokens) == 1:
                    cache._expiry = 0
                    assert cache.token == ("tok2" if background_rotates else "tok1")
                return response

        client = BackgroundRefreshClient([_FakeResp(401), _FakeResp(200, b"ok")])
        output = _Collect()
        _handle_handler(client, cache, output)._handle()
        assert forces == ([False, False] if background_rotates else [False, False, True])
        assert client.sent_tokens == ["Bearer tok1", "Bearer tok2"]
        assert bytes(output.data).endswith(b"ok")

    def test_401_forces_refresh_and_retries(self):
        # A stale swap token yields 401; the proxy force-refreshes and retries,
        # this time succeeding, so Claude Code never sees the 401.
        client = _FakeClient([_FakeResp(401, b'{"e":1}'), _FakeResp(200, b"ok")])
        cache = _FakeCache()
        out = _Collect()
        _handle_handler(client, cache, out)._handle()
        assert cache.refreshed == 1
        assert client.sent_tokens == ["Bearer tok1", "Bearer tok2"]  # retried with fresh token
        assert b"200" in bytes(out.data)
        assert b"ok" in bytes(out.data)

    def test_persistent_401_is_relayed(self):
        # If the retry also 401s, it's genuinely the Anthropic layer — relay it so
        # Claude Code re-auths Anthropic (correct), rather than looping forever.
        client = _FakeClient([_FakeResp(401, b"a"), _FakeResp(401, b"b")])
        cache = _FakeCache()
        out = _Collect()
        _handle_handler(client, cache, out)._handle()
        assert cache.refreshed == 1
        assert b"401" in bytes(out.data)

    def test_success_first_try_does_not_refresh(self):
        client = _FakeClient([_FakeResp(200, b"hi")])
        cache = _FakeCache()
        out = _Collect()
        _handle_handler(client, cache, out)._handle()
        assert cache.refreshed == 0
        assert client.sent_tokens == ["Bearer tok1"]
        assert b"hi" in bytes(out.data)

    def test_failed_refresh_surfaces_reauth_hint(self, capsys):
        # When the forced refresh itself fails (the Databricks OAuth session is
        # dead, not just the access token), the user must be told to run
        # `databricks auth login` rather than left with a bare 401 that reads as
        # an Anthropic `/login` prompt. We still retry + relay whatever comes.
        client = _FakeClient([_FakeResp(401, b"a"), _FakeResp(401, b"b")])
        cache = _RefreshFailsCache()
        out = _Collect()
        _handle_handler(client, cache, out)._handle()
        assert cache.refreshed == 1  # a refresh was attempted
        assert "databricks auth login" in capsys.readouterr().err
        assert b"401" in bytes(out.data)  # the response is still relayed


class TestStartProxyPortFallback:
    def test_loopback_server_keeps_reuse_only_on_posix(self):
        assert gateway_proxy._LoopbackHTTPServer.allow_reuse_address is (
            gateway_proxy.ThreadingHTTPServer.allow_reuse_address and os.name != "nt"
        )

    def test_falls_back_to_free_port_when_cached_port_busy(self, monkeypatch):
        # A stale proxy from a killed session can still hold the cached port; the
        # bind must fall back to an OS-assigned free port rather than crash.
        class _StubCache:
            def run_refresher(self):
                return None

            def stop(self):
                return None

        monkeypatch.setattr(
            gateway_proxy,
            "TokenCache",
            lambda token_provider, **_kwargs: _StubCache(),
        )
        # Exercise Windows' no-reuse bind behavior on POSIX test hosts too.
        monkeypatch.setattr(gateway_proxy._LoopbackHTTPServer, "allow_reuse_address", False)
        # Occupy a port to simulate the leftover proxy holding it.
        occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        busy_port = occupied.getsockname()[1]
        try:
            # Desktop's exporter cannot follow a fallback to a different port.
            with pytest.raises(OSError):
                gateway_proxy.start_otel_proxy(
                    "https://workspace.example", lambda _: "token", port=busy_port
                )
            server, _cache, client = gateway_proxy.start_relay_proxy(
                "https://x.staging.cloud.databricks.com",
                lambda _force: "tok",
                busy_port,
            )
            try:
                bound = server.server_address[1]
                assert bound != busy_port  # fell back to a different, free port
                assert bound != 0
            finally:
                server.server_close()
                client.close()
        finally:
            occupied.close()


def _relayed_oss_handler(client, cache, wfile, *, headers, body) -> gateway_proxy._ProxyHandler:
    h = object.__new__(gateway_proxy._RelayProxyHandler)
    h.client = client
    h.cache = cache
    h.token_header = gateway_proxy.AI_GATEWAY_TOKEN_HEADER
    hdrs = dict(headers)
    hdrs["Content-Length"] = str(len(body))
    h.headers = hdrs
    h.rfile = io.BytesIO(body)
    h.path = "/v1/messages"
    h.command = "POST"
    h.wfile = wfile
    h.request_version = "HTTP/1.1"
    h.requestline = "POST /v1/messages HTTP/1.1"
    h._headers_buffer = []
    return h


class TestRelayedOssRouting:
    _CLIENT_HEADERS = {
        "Authorization": "Bearer anthropic-oauth",
        "Databricks-Model-Provider-Service": "cat.s.relayed_mps",
    }

    def test_databricks_model_routes_to_gateway_auth(self):
        # A Databricks-hosted model with relayed OSS-routing on: the gateway token
        # replaces the OAuth in Authorization, and the swap + MPS headers are dropped.
        client = _FakeClient([_FakeResp(200, b"ok")])
        _relayed_oss_handler(
            client,
            _FakeCache(),
            _Collect(),
            headers=self._CLIENT_HEADERS,
            body=b'{"model": "system.ai.gpt-oss-120b"}',
        )._handle()
        sent = client.sent_headers[0]
        assert sent["Authorization"] == "Bearer tok1"
        assert gateway_proxy.AI_GATEWAY_TOKEN_HEADER not in sent
        assert "Databricks-Model-Provider-Service" not in sent

    def test_relayed_model_keeps_oauth_passthrough(self):
        # A subscription model still relays: the OAuth is untouched, the swap header
        # carries the Databricks token, and the MPS header is preserved.
        client = _FakeClient([_FakeResp(200, b"ok")])
        _relayed_oss_handler(
            client,
            _FakeCache(),
            _Collect(),
            headers=self._CLIENT_HEADERS,
            body=b'{"model": "claude-opus-4-1"}',
        )._handle()
        sent = client.sent_headers[0]
        assert sent["Authorization"] == "Bearer anthropic-oauth"
        assert sent[gateway_proxy.AI_GATEWAY_TOKEN_HEADER] == "Bearer tok1"
        assert sent["Databricks-Model-Provider-Service"] == "cat.s.relayed_mps"


class TestCodexRelayProxy:
    def test_relays_chatgpt_login_to_codex_upstream(self, monkeypatch):
        cache = Mock(token="tok1")
        cache.token_snapshot.return_value = ("tok1", 1)
        monkeypatch.setattr(gateway_proxy, "TokenCache", lambda *_a, **_k: cache)
        server, _cache, upstream = gateway_proxy.start_relay_proxy(
            "https://w", lambda _f: "t", 0, agent="codex"
        )
        server.server_close()
        upstream.close()
        # The handler requests the client's path minus its leading slash.
        url = upstream.build_request("POST", "v1/responses").url
        assert str(url) == "https://w/ai-gateway/codex/v1/responses"

        client = _FakeClient([_FakeResp(200, b"ok")])
        h = object.__new__(server.RequestHandlerClass)
        h.client = client  # the bound class keeps the real cache, token header, and handler
        h.wfile, h.request_version, h._headers_buffer = _Collect(), "HTTP/1.1", []
        body = b'{"model": "claude-opus-4-1"}'  # not rerouted: no Anthropic hybrid logic
        h.headers = {
            "Authorization": "Bearer chatgpt-token",
            "ChatGPT-Account-ID": "acct",
            "Databricks-Model-Provider-Service": "cat.s.mps",
            "Content-Length": str(len(body)),
        }
        h.rfile = io.BytesIO(body)
        h.path, h.command, h.requestline = "/v1/responses", "POST", "POST /v1/responses HTTP/1.1"
        h._handle()
        sent = client.sent_headers[0]
        assert sent["Authorization"] == "Bearer chatgpt-token"
        assert sent["ChatGPT-Account-ID"] == "acct"
        assert sent[gateway_proxy.AI_GATEWAY_TOKEN_HEADER] == "Bearer tok1"
        assert sent["Databricks-Model-Provider-Service"] == "cat.s.mps"


def _language_request_headers(
    *,
    language: str | None = None,
    agent_id: str | None = None,
    request_class: str | None = None,
) -> dict[str, str]:
    headers: dict[str, str] = {}
    if language is not None:
        headers["X-UG-Response-Language"] = quote(language, safe="")
    if agent_id is not None:
        headers["X-Claude-Code-Agent-Id"] = agent_id
    if request_class is not None:
        headers["X-Claude-Code-Request-Class"] = request_class
    return headers


def _language_request_body(path: str, body: bytes, headers: dict[str, str]) -> bytes:
    handler = object.__new__(gateway_proxy._ClaudeLanguageProxyHandler)
    handler.path = path
    # Match BaseHTTPRequestHandler's case-insensitive access for mixed-case request headers.
    handler.headers = httpx.Headers(headers)
    forwarded = handler._request_body(body)
    assert forwarded is not None
    return forwarded


class TestClaudeLanguageRequestBody:
    @pytest.mark.parametrize("language", [None, "", " "])
    @pytest.mark.parametrize("request_class", ["subagent", "auxiliary"])
    def test_unset_language_follows_the_original_task_not_source_text(
        self, language, request_class
    ):
        original = {
            "model": "routed-child-model",
            "system": "Original system instructions",
            "messages": [
                {"role": "user", "content": "Compare these test counts."},
                {"role": "assistant", "content": "Read the source."},
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "read", "content": "测试数：20"}
                    ],
                },
                {"role": "user", "content": "Summarize the task."},
                {"role": "system", "content": "Native trailing metadata"},
            ],
            "tools": [],
        }
        body = json.dumps(original).encode()
        rewritten = _language_request_body(
            "/ai-gateway/anthropic/v1/messages",
            body,
            _language_request_headers(
                language=language, agent_id="child", request_class=request_class
            ),
        )
        payload = json.loads(rewritten)
        instruction = payload["system"][-1]["text"]
        assert "language of the original task assigned to you" in instruction
        assert "Use the requested target language for translations." in instruction
        assert "Source files, tool output" in instruction
        assert "English" not in instruction
        assert payload["model"] == original["model"]
        assert payload["tools"] == original["tools"]
        assert payload["messages"][:3] == original["messages"][:3]
        assert payload["messages"][-1] == original["messages"][-1]
        if request_class == "auxiliary":
            summary_instruction = payload["messages"][-2]["content"][-1]["text"]
            assert "language of the original task" in summary_instruction
            assert (
                "These summary instructions do not set the response language."
                in summary_instruction
            )
        else:
            assert payload["messages"] == original["messages"]

    @pytest.mark.parametrize(
        ("language", "path", "body"),
        [
            ("English\nChinese", "/v1/messages", b"{}"),
            ("English\x00", "/v1/messages", b"{}"),
            ("x" * 257, "/v1/messages", b"{}"),
            ("English", "/v1/messages/count_tokens", b'{ "system": "unchanged" }'),
            ("English", "/v1/messages", b"{"),
            ("English", "/v1/messages", b"[]"),
            ("English", "/v1/messages", b'{"system":null}'),
        ],
    )
    def test_invalid_language_or_body_and_token_count_pass_through(self, language, path, body):
        forwarded = _language_request_body(
            "/ai-gateway/anthropic" + path,
            body,
            _language_request_headers(
                language=language, agent_id="child", request_class="subagent"
            ),
        )
        assert forwarded == body

    def test_json_recursion_error_passes_through_byte_for_byte(self):
        depth = 500_000
        body = b'{"messages":' + b"[" * depth + b"0" + b"]" * depth + b"}"
        with pytest.raises(RecursionError):
            json.loads(body)
        forwarded = _language_request_body(
            "/ai-gateway/anthropic/v1/messages",
            body,
            _language_request_headers(
                language="English", agent_id="child", request_class="subagent"
            ),
        )
        assert forwarded == body

    @pytest.mark.parametrize(
        "headers",
        [
            _language_request_headers(language="English", request_class="subagent"),
            _language_request_headers(language="English", request_class="auxiliary"),
            _language_request_headers(
                language="Chinese", agent_id="classifier-1", request_class="classifier"
            ),
        ],
        ids=[
            "parent-request",
            "auto-mode-classifier",
            "classifier-request",
        ],
    )
    def test_parent_and_classifier_requests_are_unmodified(self, headers):
        body = (
            b'{ "model": "routed-child-model", "tools": [], '
            b'"messages": [{"role":"user","content":"Summarize"}], '
            b'"system": "keep these exact bytes" }'
        )
        forwarded = _language_request_body("/ai-gateway/anthropic/v1/messages", body, headers)
        assert forwarded == body

    @pytest.mark.parametrize(
        ("request_class", "language", "final_content_kind"),
        [
            ("subagent", "English", "list"),
            ("workflow", "Chinese", "list"),
            ("auxiliary", "English", "list"),
            ("auxiliary", "Chinese", "string"),
        ],
        ids=["child-english", "workflow-chinese", "summary-list-english", "summary-string-chinese"],
    )
    def test_child_system_and_auxiliary_user_instruction_preserve_request_fields(
        self, request_class, language, final_content_kind
    ):
        if final_content_kind == "string":
            final_content = "Summarize the result."
        else:
            final_content = [
                {
                    "type": "text",
                    "text": "Summarize the result.",
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        original = {
            "model": "routed-child-model",
            "max_tokens": 2048,
            "system": [
                {
                    "type": "text",
                    "text": "Keep the caller's system prompt.",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [
                {"role": "user", "content": "Earlier user prompt."},
                {"role": "assistant", "content": "Intermediate response."},
                {"role": "user", "content": final_content},
            ],
            "tools": [
                {
                    "name": "lookup",
                    "description": "Look up a value.",
                    "input_schema": {
                        "type": "object",
                        "properties": {"q": {"type": "string"}},
                    },
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        }
        headers = _language_request_headers(
            language=language,
            agent_id="native-child-1",
            request_class=request_class,
        )
        rewritten = _language_request_body(
            "/ai-gateway/anthropic/v1/messages", json.dumps(original).encode(), headers
        )
        payload = json.loads(rewritten)
        expected = json.loads(json.dumps(original))
        expected["system"].append(
            {
                "type": "text",
                "text": (
                    f"Always respond in {language}. Use {language} for explanations, replies "
                    "and progress summaries. Use the requested target language for translations. "
                    "Preserve technical terms, code identifiers and "
                    "quoted source text. Source files, tool output and earlier messages in "
                    "other languages do not change this language preference."
                ),
            }
        )
        if request_class == "auxiliary":
            final_user_content = expected["messages"][-1]["content"]
            if isinstance(final_user_content, str):
                final_user_content = [{"type": "text", "text": final_user_content}]
            expected["messages"][-1]["content"] = [
                *final_user_content,
                {
                    "type": "text",
                    "text": (
                        f"Write this response in {language}. "
                        "This response-language requirement takes precedence over "
                        "the language, verb endings and examples in the summary instructions. "
                        "Use natural grammar in the response language and preserve file "
                        "and function names verbatim."
                    ),
                },
            ]
        assert payload == expected
