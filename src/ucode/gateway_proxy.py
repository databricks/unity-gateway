"""Loopback refresh proxy for Claude gateway requests.

A relayed Model Provider Service authenticates the caller's own Anthropic
subscription OAuth (which Claude Code owns in the `Authorization` header) and
carries a Databricks credential in the `X-Databricks-AI-Gateway-Token` swap
header. Native gateway discovery instead carries the Databricks credential in
`Authorization`. The proxy refreshes the applicable header and streams responses
back verbatim.

With relayed OSS-routing on, the proxy picks per request by the requested model:
Databricks-hosted ids (system.ai / OSS) take the gateway-auth path while relayed
subscription models keep the OAuth passthrough, so one Claude Code session can use
both.

The gateway-auth proxy serves agents that can only hold a static credential
(Copilot CLI): the agent authenticates to the proxy with a per-session secret,
and the proxy swaps it for a fresh Databricks token on allowlisted gateway paths.

Security invariants (mirroring `databricks.py` token handling):
  - Binds 127.0.0.1 only; never exposed off-host.
  - Never logs header values or bodies. The Databricks token lives in memory,
    refreshed off the request path; the Anthropic OAuth in `Authorization` is
    passed through untouched in relayed mode and never logged.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import os
import re
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast
from urllib.parse import unquote, urlsplit

import httpx

from ucode.env import env_bool
from ucode.smart_routing.codex_v2_transport import (
    prepare_request,
    transform_response_event,
)

# Header we overwrite with the freshly-minted Databricks credential. Any
# client-supplied value is replaced, so a stale settings.json value can't leak.
AI_GATEWAY_TOKEN_HEADER = "X-Databricks-AI-Gateway-Token"
AUTHORIZATION_HEADER = "Authorization"
# Header that routes a request to a specific Model Provider Service. Dropped when a
# request is re-routed to a Databricks-hosted model so the gateway serves it directly.
MODEL_PROVIDER_SERVICE_HEADER = "Databricks-Model-Provider-Service"
# Hop-by-hop headers must not be forwarded across a proxy.
HOP_BY_HOP_HEADERS = frozenset(
    h.lower()
    for h in (
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    )
)
# Per-operation upstream timeouts. `read` is generous because model turns stream
# over a single response and Anthropic emits SSE pings, so inter-chunk gaps stay
# small; `connect`/`pool` fail fast when the gateway is unreachable.
UPSTREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=600.0, pool=10.0)
# Refresh once the token has less than this many seconds of life left. Databricks
# access tokens live ~1h; a 10-min buffer leaves ample headroom for a retry.
_REFRESH_BUFFER_S = 600
# How often the background thread re-checks freshness. Cheap: it only shells out
# to the CLI when actually within the buffer, otherwise it's a bare clock compare.
_REFRESHER_POLL_S = 120
# Assumed lifetime when a token carries no decodable `exp` (defensive fallback).
_DEFAULT_TTL_S = 3600
# Opt-in transport diagnostics for intermittent streaming failures. Events only
# contain locally-generated request ids, timings, status codes, byte counts,
# and exception class names — never headers, bodies, or credentials.
_DIAGNOSTICS_ENV = "UCODE_RELAYED_PROXY_DIAGNOSTICS"
_CODEX_V2_MAX_SSE_EVENT_BYTES = 16 * 1024 * 1024


def _diagnostics_enabled() -> bool:
    return env_bool(_DIAGNOSTICS_ENV)


def log_proxy_diagnostic(event: str, **fields: object) -> None:
    if not _diagnostics_enabled():
        return
    payload = {"event": event, **fields}
    sys.stderr.write(
        f"[ucode-relay] {json.dumps(payload, sort_keys=True, separators=(',', ':'))}\n"
    )
    sys.stderr.flush()


def _jwt_exp(token: str) -> float | None:
    """Best-effort `exp` (epoch seconds) from a JWT access token, else None."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # restore base64 padding
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except (IndexError, ValueError, KeyError, binascii.Error, json.JSONDecodeError):
        return None


def log_token_refresh_failure(exc: BaseException) -> None:
    """Surface (never silently swallow) a refresh failure, without leaking any
    token or header value."""
    sys.stderr.write(
        f"[ucode] Databricks token refresh failed: {exc}. If the session stalls, "
        "run `databricks auth login` for your workspace profile.\n"
    )


class TokenCache:
    """Holds the current Databricks token and its expiry, refreshing lazily as it
    nears expiry.

    A background thread refreshes proactively so the request path rarely blocks,
    but the request path also refreshes on demand — which is what carries the
    token across events the timer can't (laptop sleep suspends the monotonic
    clock, so a fixed interval silently stops advancing). All refreshes are
    single-flighted through ``_refresh_lock`` so a burst of requests at the expiry
    boundary triggers exactly one CLI call, not a thundering herd on the shared
    token cache."""

    def __init__(
        self,
        token_provider: Callable[[bool], str],
        *,
        force_refresh_near_expiry: bool = False,
        on_refresh_failure: Callable[[BaseException], None] = log_token_refresh_failure,
    ) -> None:
        # token_provider(force_refresh) mints a fresh token from the same source the
        # client agent uses, so the proxy authenticates as the same principal (e.g.
        # a per-user custom-OAuth token, not the default CLI profile).
        self._token_provider = token_provider
        self._force_refresh_near_expiry = force_refresh_near_expiry
        # Agents that own the terminal (a TUI) need failures kept off stderr.
        self._on_refresh_failure = on_refresh_failure
        self._state_lock = threading.Lock()  # guards _token / _expiry (brief)
        self._refresh_lock = threading.Lock()  # single-flights the CLI refresh
        self._stop = threading.Event()
        self._token = ""
        self._expiry = 0.0
        # Preserve the existing non-forced relayed-auth fetch. Gateway discovery
        # opts into a forced fetch so its static client token starts with a full TTL.
        self._refresh(force=force_refresh_near_expiry)

    def _refresh(self, *, force: bool) -> None:
        """Mint a token and record its expiry."""
        token = self._token_provider(force)
        expiry = _jwt_exp(token) or (time.time() + _DEFAULT_TTL_S)
        with self._state_lock:
            self._token = token
            self._expiry = expiry

    def _fresh_enough(self) -> bool:
        with self._state_lock:
            return bool(self._token) and time.time() < self._expiry - _REFRESH_BUFFER_S

    def _ensure_fresh(self) -> None:
        if self._fresh_enough():
            return
        with self._refresh_lock:
            if self._fresh_enough():  # another thread refreshed while we waited
                return
            try:
                self._refresh(force=self._force_refresh_near_expiry)
            except RuntimeError as exc:
                # Keep serving the current token; a request that then 401s triggers
                # a forced refresh + retry (see _ProxyHandler._handle).
                self._on_refresh_failure(exc)

    @property
    def token(self) -> str:
        self._ensure_fresh()
        with self._state_lock:
            return self._token

    def report_refresh_failure(self, exc: BaseException) -> None:
        self._on_refresh_failure(exc)

    def refresh(self) -> None:
        """Force a fresh mint now (used by the retry-on-401 path)."""
        with self._refresh_lock:
            self._refresh(force=True)

    def run_refresher(self) -> None:
        while not self._stop.wait(_REFRESHER_POLL_S):
            try:
                self._ensure_fresh()
            except Exception as exc:  # noqa: BLE001 - a stray error must NOT kill the thread
                # If this thread dies, nothing refreshes and the session lapses at
                # the ~1h mark until restart. Log and keep looping instead.
                self._on_refresh_failure(exc)

    def stop(self) -> None:
        self._stop.set()


class _CodexV2ProtocolError(RuntimeError):
    """A malformed or unsafe native Codex v2 wire message."""


def forwarded_request_headers(
    handler: BaseHTTPRequestHandler,
    token: str,
    token_header: str = AI_GATEWAY_TOKEN_HEADER,
    extra_strip: frozenset[str] = frozenset(),
) -> dict[str, str]:
    strip_on_forward = HOP_BY_HOP_HEADERS | {token_header.lower()} | extra_strip
    headers = {
        key: value for key, value in handler.headers.items() if key.lower() not in strip_on_forward
    }
    headers[token_header] = f"Bearer {token}"
    return headers


# On the Databricks-hosted path the gateway credential goes in `Authorization` (so the
# caller's Anthropic OAuth is replaced), and the swap + MPS headers are dropped so the
# gateway serves the model directly instead of relaying to the subscription MPS.
_DATABRICKS_ROUTE_STRIP = frozenset(
    {AI_GATEWAY_TOKEN_HEADER.lower(), MODEL_PROVIDER_SERVICE_HEADER.lower()}
)


def is_databricks_routed_model(model: str | None) -> bool:
    """True when ``model`` is a Databricks-hosted (gateway-served) id rather than a model
    the relayed Anthropic subscription serves.

    Databricks ids are namespace-qualified (``system.ai.*``, ``catalog.schema.model``,
    ``databricks-*``); the relayed subscription uses Anthropic's bare canonical names
    (``claude-opus-4-1``, ``claude-sonnet-4-5``, ...), which never carry a dot."""
    if not model:
        return False
    return "." in model or model.startswith("databricks-")


def _request_model(body: bytes | None) -> str | None:
    """The ``model`` field of a JSON request body, or None when absent/unparseable."""
    if not body:
        return None
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return None
    model = payload.get("model") if isinstance(payload, dict) else None
    return model if isinstance(model, str) else None


class _ProxyHandler(BaseHTTPRequestHandler):
    # Set by the server factory.
    cache: TokenCache
    client: httpx.Client
    token_header = AI_GATEWAY_TOKEN_HEADER

    def log_message(self, format: str, *args: object) -> None:
        return

    def _safe_send_error(self, code: int, message: str) -> None:
        # The client (Claude Code) may already have disconnected, in which case
        # reporting the error writes to a dead socket and raises again; swallow it.
        try:
            self.send_error(code, message)
        except OSError:
            pass

    def _prepare_request_body(self, body: bytes | None) -> bytes | None:
        return body

    def _response_headers(self, resp: httpx.Response) -> tuple[tuple[str, str], ...]:
        return tuple(
            (key, value)
            for key, value in resp.headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS
        )

    def _iter_response_chunks(self, resp: httpx.Response):
        yield from resp.iter_raw()

    def _forward_target(self, body: bytes | None) -> tuple[str, frozenset[str], str]:
        return self.token_header, frozenset(), "forward"

    def _request_body(self, body: bytes | None) -> bytes | None:
        """The body to forward; ``body`` unchanged unless a subclass rewrites it."""
        return body

    def _added_headers(self) -> dict[str, str]:
        return {}

    def _on_refresh_failure(self, exc: RuntimeError) -> bool:
        """Report a failed forced refresh; True means a response was already sent."""
        log_token_refresh_failure(exc)
        return False

    def _handle(self) -> None:
        diagnostic_id = uuid.uuid4().hex[:12]
        started = time.monotonic()
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self._request_body(self.rfile.read(length) if length else None)
        url = self.path.lstrip("/")
        try:
            body = self._prepare_request_body(body)
        except ValueError as exc:
            # Adapter failures are deliberately sanitized: neither the request
            # body nor credentials belong in diagnostics or the client error.
            log_proxy_diagnostic(
                "request_protocol_error",
                request_id=diagnostic_id,
                error_type=type(exc).__name__,
            )
            self._safe_send_error(400, "codex v2 request protocol error")
            return
        token_header, extra_strip, route_label = self._forward_target(body)
        log_proxy_diagnostic(
            "request_start",
            request_id=diagnostic_id,
            method=self.command,
            path=self.path.split("?", 1)[0],
            route=route_label,
        )

        def request_headers() -> dict[str, str]:
            headers = forwarded_request_headers(
                self, self.cache.token, token_header, extra_strip=extra_strip
            )
            headers.update(self._added_headers())
            return headers

        try:
            # First attempt with the current token.
            headers = request_headers()
            with self.client.stream(self.command, url, headers=headers, content=body) as resp:
                log_proxy_diagnostic(
                    "upstream_headers",
                    request_id=diagnostic_id,
                    attempt=1,
                    status=resp.status_code,
                    elapsed_ms=round((time.monotonic() - started) * 1000),
                )
                if resp.status_code not in (401, 403):
                    self._relay_response(resp, diagnostic_id=diagnostic_id, started=started)
                    return
                # Auth rejected. Drain the (small) error body so the pooled
                # connection can be reused, then fall through to one retry.
                resp.read()
            # A relayed 401/403 may be a stale Databricks swap token rather than a
            # bad Anthropic OAuth — the two are indistinguishable from the status
            # alone. Force-refresh the Databricks token and retry once. If it was the
            # Anthropic layer, the retry still 401s and we relay it verbatim, so a
            # genuine re-auth is triggered; a stale-Databricks 401 self-heals here
            # instead of surfacing to Claude Code as a spurious Anthropic prompt.
            try:
                self.cache.refresh()
            except RuntimeError as exc:
                # Refresh failed: the Databricks OAuth session is dead (not just the
                # access token) and can't be re-minted non-interactively. Surface the
                # `databricks auth login` hint rather than silently relaying a bare 401,
                # which otherwise reads as an Anthropic `/login` prompt and sends the
                # user to the wrong re-auth. Still retry + relay with the existing token.
                if self._on_refresh_failure(exc):
                    return
            headers = request_headers()
            with self.client.stream(self.command, url, headers=headers, content=body) as resp:
                log_proxy_diagnostic(
                    "upstream_headers",
                    request_id=diagnostic_id,
                    attempt=2,
                    status=resp.status_code,
                    elapsed_ms=round((time.monotonic() - started) * 1000),
                )
                self._relay_response(resp, diagnostic_id=diagnostic_id, started=started)
        except (BrokenPipeError, ConnectionResetError):
            # Client closed before/while we relayed headers — routine on cancel.
            log_proxy_diagnostic(
                "client_disconnect",
                request_id=diagnostic_id,
                phase="request",
                elapsed_ms=round((time.monotonic() - started) * 1000),
            )
            return
        except httpx.HTTPError as exc:
            # Upstream failed before any bytes reached the client; a 502 is still
            # sendable. (An HTTP *status* like 429 is not an error here — httpx
            # only raises for transport failures — so real gateway errors are
            # relayed verbatim by `_relay_response`.)
            log_proxy_diagnostic(
                "upstream_request_error",
                request_id=diagnostic_id,
                error_type=type(exc).__name__,
                elapsed_ms=round((time.monotonic() - started) * 1000),
            )
            self._safe_send_error(502, "gateway proxy upstream error")

    # Streaming passthrough: forward chunks as they arrive so SSE token streaming
    # is not buffered (buffering would add full-response latency to first token).
    # `iter_raw` preserves any Content-Encoding verbatim (we relay that header),
    # so the proxy stays byte-transparent.
    def _relay_response(
        self,
        resp: httpx.Response,
        *,
        diagnostic_id: str | None = None,
        started: float | None = None,
    ) -> None:
        started = started if started is not None else time.monotonic()
        chunks = 0
        bytes_relayed = 0
        first_byte_ms: int | None = None
        try:
            self.send_response(resp.status_code)
            for key, value in self._response_headers(resp):
                self.send_header(key, value)
            self.end_headers()
            # Do not pass a fixed chunk size here. httpx accumulates bytes until
            # that size is reached, which can hide small SSE heartbeat frames
            # from Claude Code for minutes during a slow artifact/tool call.
            # With ``chunk_size=None`` (the default), raw upstream chunks are
            # yielded as they arrive and pings keep the downstream connection
            # alive even before the model produces a large content block.
            for chunk in self._iter_response_chunks(resp):
                if chunk:
                    if first_byte_ms is None:
                        first_byte_ms = round((time.monotonic() - started) * 1000)
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    chunks += 1
                    bytes_relayed += len(chunk)
            log_proxy_diagnostic(
                "response_complete",
                request_id=diagnostic_id,
                status=resp.status_code,
                chunks=chunks,
                bytes=bytes_relayed,
                first_byte_ms=first_byte_ms,
                elapsed_ms=round((time.monotonic() - started) * 1000),
            )
        except (BrokenPipeError, ConnectionResetError):
            # Client (Claude Code) closed the connection mid-response — routine on
            # cancelled turns / SSE teardown. Nothing left to relay to, so stop
            # quietly rather than crashing the handler thread.
            log_proxy_diagnostic(
                "client_disconnect",
                request_id=diagnostic_id,
                phase="response",
                chunks=chunks,
                bytes=bytes_relayed,
                elapsed_ms=round((time.monotonic() - started) * 1000),
            )
            return
        except _CodexV2ProtocolError as exc:
            # The status and headers are already committed for streaming; stop
            # without relaying the malformed or unsafe event payload.
            log_proxy_diagnostic(
                "response_protocol_error",
                request_id=diagnostic_id,
                error_type=type(exc).__name__,
                status=resp.status_code,
                chunks=chunks,
                bytes=bytes_relayed,
                elapsed_ms=round((time.monotonic() - started) * 1000),
            )
            return
        except httpx.HTTPError as exc:
            # Upstream dropped mid-stream. Headers (and status) may already be
            # sent, so we can't reliably signal a fresh error — stop and let the
            # client see a truncated stream rather than corrupt the framing.
            log_proxy_diagnostic(
                "upstream_stream_error",
                request_id=diagnostic_id,
                error_type=type(exc).__name__,
                status=resp.status_code,
                chunks=chunks,
                bytes=bytes_relayed,
                elapsed_ms=round((time.monotonic() - started) * 1000),
            )
            return

    # Forward every method: this is a transparent pass-through, so routing any
    # `do_<METHOD>` lookup to `_handle` lets the gateway reject unsupported methods.
    def __getattr__(self, name: str):
        if name.startswith("do_"):
            return self._handle
        raise AttributeError(name)


class _RelayProxyHandler(_ProxyHandler):
    def _forward_target(self, body: bytes | None) -> tuple[str, frozenset[str], str]:
        if is_databricks_routed_model(_request_model(body)):
            return AUTHORIZATION_HEADER, _DATABRICKS_ROUTE_STRIP, "databricks"
        return self.token_header, frozenset(), "relay"


class _CodexV2ProxyHandler(_ProxyHandler):
    """Loopback adapter for native Codex v2 Responses traffic."""

    def _prepare_request_body(self, body: bytes | None) -> bytes | None:
        self._codex_v2_adapter_active = False
        if self.command != "POST" or urlsplit(self.path).path != "/v1/responses" or body is None:
            return body
        transformed, changed = prepare_request(body)
        self._codex_v2_adapter_active = changed
        return transformed

    def _forward_target(self, body: bytes | None) -> tuple[str, frozenset[str], str]:
        # Use the same Databricks identity, without a competing swap credential.
        return (
            AUTHORIZATION_HEADER,
            frozenset({AI_GATEWAY_TOKEN_HEADER.lower()}),
            "codex-v2",
        )

    def _response_type(self, resp: httpx.Response) -> str:
        if self._codex_v2_adapter_active and 200 <= resp.status_code < 300:
            return resp.headers.get("content-type", "").lower()
        return ""

    def _response_headers(self, resp: httpx.Response) -> tuple[tuple[str, str], ...]:
        content_type = self._response_type(resp)
        decoded = "json" in content_type or "text/event-stream" in content_type
        return tuple(
            (key, value)
            for key, value in super()._response_headers(resp)
            if not (decoded and key.lower() == "content-encoding")
        )

    def _iter_response_chunks(self, resp: httpx.Response):
        content_type = self._response_type(resp)
        if "text/event-stream" in content_type:
            yield from self._iter_sse_chunks(resp)
            return
        if "json" not in content_type:
            yield from resp.iter_raw()
            return

        body = bytearray()
        for chunk in resp.iter_bytes():
            body.extend(chunk)
            if len(body) > _CODEX_V2_MAX_SSE_EVENT_BYTES:
                raise _CodexV2ProtocolError("Codex v2 JSON response exceeds the bounded limit")
        if not body:
            return
        try:
            event = json.loads(bytes(body))
            transformed = transform_response_event(event)
        except (TypeError, ValueError) as exc:
            raise _CodexV2ProtocolError("Codex v2 JSON response failed adaptation") from exc
        if transformed == event:
            yield bytes(body)
        else:
            yield json.dumps(transformed, ensure_ascii=False, separators=(",", ":")).encode()

    def _iter_sse_chunks(self, resp: httpx.Response):
        buffer = bytearray()
        for chunk in resp.iter_bytes():
            buffer.extend(chunk)
            while separator := re.search(rb"\r\n\r\n|\n\n", buffer):
                frame_size = separator.end()
                if frame_size > _CODEX_V2_MAX_SSE_EVENT_BYTES:
                    raise _CodexV2ProtocolError("Codex v2 SSE event exceeds the bounded limit")
                frame = bytes(buffer[:frame_size])
                del buffer[:frame_size]
                yield self._transform_sse_frame(frame)
            if len(buffer) > _CODEX_V2_MAX_SSE_EVENT_BYTES:
                raise _CodexV2ProtocolError("Codex v2 SSE event exceeds the bounded limit")
        if buffer:
            # Never execute a partial function call at EOF.
            if buffer.strip(b"\r\n :"):
                raise _CodexV2ProtocolError("Codex v2 SSE stream ended mid-event")
            yield bytes(buffer)

    @staticmethod
    def _transform_sse_frame(frame: bytes) -> bytes:
        lines = frame.splitlines(keepends=True)
        event_type: str | None = None
        data_indices: list[int] = []
        for index, line in enumerate(lines):
            if line.startswith(b"event:"):
                try:
                    event_type = line[6:].strip().decode("utf-8", "strict")
                except UnicodeDecodeError as exc:
                    raise _CodexV2ProtocolError("Codex v2 SSE event name is malformed") from exc
            elif line.startswith(b"data:"):
                data_indices.append(index)
        if not data_indices:
            return frame
        payload = b"\n".join(lines[i][5:].rstrip(b"\r\n").lstrip() for i in data_indices)
        if payload == b"[DONE]":
            return frame
        try:
            event = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            if event_type is not None and event_type.startswith("response."):
                raise _CodexV2ProtocolError("Codex v2 SSE response event is malformed") from exc
            return frame
        if not isinstance(event, dict):
            if event_type is not None and event_type.startswith("response."):
                raise _CodexV2ProtocolError("Codex v2 SSE response event is not an object")
            return frame

        try:
            transformed = transform_response_event(event)
        except (TypeError, ValueError) as exc:
            raise _CodexV2ProtocolError("Codex v2 SSE response failed adaptation") from exc
        if transformed == event:
            return frame
        serialized = json.dumps(transformed, ensure_ascii=False, separators=(",", ":")).encode()
        first = data_indices[0]
        ending = b"\r\n" if lines[first].endswith(b"\r\n") else b"\n"
        lines[first] = b"data: " + serialized + ending
        for index in data_indices[1:]:
            lines[index] = b""
        return b"".join(lines)


# Gateway model routes a gateway-auth proxy session may reach. Everything else
# (workspace REST APIs included) is refused, so the session secret only buys inference.
GATEWAY_MODEL_PATH_PREFIXES = (
    "/ai-gateway/anthropic/",
    "/ai-gateway/mlflow/v1/",
    "/ai-gateway/openai/v1/",
)
_ANTHROPIC_PATH_PREFIX = "/ai-gateway/anthropic/"
# The agent's credential headers carry the session secret, never a Databricks token.
_SESSION_CREDENTIAL_HEADERS = frozenset({"authorization", "x-api-key"})


def _is_allowed_gateway_path(path: str, prefixes: tuple[str, ...]) -> bool:
    decoded = path.split("?", 1)[0]
    # Decode until stable so a double-encoded dot segment can't reach another API.
    while (next_decoded := unquote(decoded)) != decoded:
        decoded = next_decoded
    if any(segment in (".", "..") for segment in decoded.split("/")):
        return False
    return decoded.startswith(prefixes)


def _bearer_value(value: str | None) -> str | None:
    if not value:
        return None
    scheme, _, credential = value.partition(" ")
    return credential.strip() if scheme.lower() == "bearer" else None


class _GatewayAuthProxyHandler(_ProxyHandler):
    # Set by the server factory.
    session_secret: str
    added_headers: Mapping[str, str]
    allowed_path_prefixes: tuple[str, ...]
    sign_in_hint: str
    dropped_chat_completions_fields: frozenset[str]
    path_rewriter: Callable[[str], str] | None = None
    body_rewriter: Callable[[str, bytes], bytes] | None = None

    def _request_body(self, body: bytes | None) -> bytes | None:
        if body and self.body_rewriter is not None:
            body = self.body_rewriter(self.path, body)
        if not body or not self.dropped_chat_completions_fields:
            return body
        if not self.path.split("?", 1)[0].endswith("/chat/completions"):
            return body
        try:
            payload = json.loads(body)
        except ValueError:
            return body
        if not isinstance(payload, dict) or self.dropped_chat_completions_fields.isdisjoint(
            payload
        ):
            return body
        for field in self.dropped_chat_completions_fields:
            payload.pop(field, None)
        return json.dumps(payload).encode()

    def _has_session_secret(self) -> bool:
        expected = self.session_secret.encode()
        for presented in (
            _bearer_value(self.headers.get(AUTHORIZATION_HEADER)),
            self.headers.get("x-api-key"),
        ):
            if presented and hmac.compare_digest(presented.encode(), expected):
                return True
        return False

    def _reject(self, status: int, message: str) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length:
            self.rfile.read(length)
        self._send_json_error(status, message)

    def _send_json_error(self, status: int, message: str) -> None:
        # Shaped like the upstream dialect so the agent shows the message instead of
        # a generic transport failure.
        if self.path.startswith(_ANTHROPIC_PATH_PREFIX):
            error_type = "authentication_error" if status == 401 else "not_found_error"
            payload = {"type": "error", "error": {"type": error_type, "message": message}}
        else:
            payload = {"error": {"message": message, "type": "invalid_request_error"}}
        body = json.dumps(payload).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass

    def _handle(self) -> None:
        if not _is_allowed_gateway_path(self.path, self.allowed_path_prefixes):
            self._reject(404, "This path is not served by the Unity Gateway proxy.")
            return
        if not self._has_session_secret():
            self._reject(401, "Missing or invalid Unity Gateway session credential.")
            return
        if self.path_rewriter is not None:
            rewritten = self.path_rewriter(self.path)
            # A rewrite must stay on a path the session may reach.
            if not _is_allowed_gateway_path(rewritten, self.allowed_path_prefixes):
                self._reject(404, "This path is not served by the Unity Gateway proxy.")
                return
            self.path = rewritten
        super()._handle()

    def _forward_target(self, body: bytes | None) -> tuple[str, frozenset[str], str]:
        added = frozenset(name.lower() for name in self.added_headers)
        return self.token_header, _SESSION_CREDENTIAL_HEADERS | added, "gateway"

    def _added_headers(self) -> dict[str, str]:
        return dict(self.added_headers)

    def _on_refresh_failure(self, exc: RuntimeError) -> bool:
        self.cache.report_refresh_failure(exc)
        self._send_json_error(401, f"Databricks sign-in expired. {self.sign_in_hint}")
        return True


class _LoopbackHTTPServer(ThreadingHTTPServer):
    # Windows permits sharing an active listener when SO_REUSEADDR is enabled.
    allow_reuse_address = ThreadingHTTPServer.allow_reuse_address and os.name != "nt"


def _start_proxy(
    workspace: str,
    token_provider: Callable[[bool], str],
    port: int,
    *,
    upstream_path: str,
    token_header: str,
    handler_type: type[_ProxyHandler],
    force_refresh_near_expiry: bool,
    fallback_on_bind_error: bool = True,
    handler_attrs: Mapping[str, object] | None = None,
    on_refresh_failure: Callable[[BaseException], None] = log_token_refresh_failure,
) -> tuple[ThreadingHTTPServer, TokenCache, httpx.Client]:
    """Start the loopback refresh proxy + its background token refresher.

    Binds ``port``, optionally falling back to an OS-assigned port when occupied.
    The caller reads ``server.server_address[1]`` for the actual port. Desktop
    disables fallback because its exporter keeps the configured port.

    ``token_provider(force_refresh)`` mints the token from the same source the
    client agent authenticates with.

    Returns (server, cache, client); the caller runs the server (e.g. in a
    thread) and calls shutdown()/cache.stop()/client.close() on exit.
    """
    upstream_base = f"{workspace.rstrip('/')}/{upstream_path.lstrip('/')}"
    cache = TokenCache(
        token_provider,
        force_refresh_near_expiry=force_refresh_near_expiry,
        on_refresh_failure=on_refresh_failure,
    )
    # One pooled, keep-alive client shared across handler threads: reuses TCP+TLS
    # to the gateway instead of a fresh handshake per request. Don't follow
    # redirects — a proxy relays 3xx verbatim.
    client = httpx.Client(base_url=upstream_base, timeout=UPSTREAM_TIMEOUT, follow_redirects=False)

    handler = cast(
        type[_ProxyHandler],
        type(
            "BoundProxyHandler",
            (handler_type,),
            {
                "cache": cache,
                "client": client,
                "token_header": token_header,
                **(handler_attrs or {}),
            },
        ),
    )
    try:
        server = _LoopbackHTTPServer(("127.0.0.1", port), handler)
    except OSError:
        if not fallback_on_bind_error:
            cache.stop()
            client.close()
            raise
        # Cached port is occupied (stale proxy from a killed session). Port 0 lets
        # the OS pick any free port; the caller reconciles the base URL to it.
        server = _LoopbackHTTPServer(("127.0.0.1", 0), handler)

    refresher = threading.Thread(target=cache.run_refresher, daemon=True)
    refresher.start()
    return server, cache, client


def start_relay_proxy(
    workspace: str,
    token_provider: Callable[[bool], str],
    port: int,
) -> tuple[ThreadingHTTPServer, TokenCache, httpx.Client]:
    """Start the Claude subscription relay proxy."""
    return _start_proxy(
        workspace,
        token_provider,
        port,
        upstream_path="ai-gateway/anthropic/",
        token_header=AI_GATEWAY_TOKEN_HEADER,
        handler_type=_RelayProxyHandler,
        force_refresh_near_expiry=False,
    )


def start_otel_proxy(
    workspace: str,
    token_provider: Callable[[bool], str],
    *,
    port: int = 0,
) -> tuple[ThreadingHTTPServer, TokenCache, httpx.Client]:
    """Start the Codex OTLP proxy, optionally on desktop's persistent port."""
    return _start_proxy(
        workspace,
        token_provider,
        port,
        upstream_path="ai-gateway/otel/",
        token_header=AUTHORIZATION_HEADER,
        handler_type=_ProxyHandler,
        force_refresh_near_expiry=True,
        fallback_on_bind_error=False,
    )


def start_codex_v2_proxy(
    workspace: str,
    token_provider: Callable[[bool], str],
) -> tuple[ThreadingHTTPServer, TokenCache, httpx.Client]:
    """Start the opt-in native Codex v2 plaintext adapter on a loopback port."""
    return _start_proxy(
        workspace,
        token_provider,
        0,
        upstream_path="ai-gateway/codex/",
        token_header=AUTHORIZATION_HEADER,
        handler_type=_CodexV2ProxyHandler,
        force_refresh_near_expiry=True,
    )


def start_gateway_auth_proxy(
    workspace: str,
    token_provider: Callable[[bool], str],
    *,
    session_secret: str,
    added_headers: Mapping[str, str],
    sign_in_hint: str,
    on_refresh_failure: Callable[[BaseException], None] = log_token_refresh_failure,
    dropped_chat_completions_fields: Iterable[str] = (),
    allowed_path_prefixes: tuple[str, ...] = GATEWAY_MODEL_PATH_PREFIXES,
    path_rewriter: Callable[[str], str] | None = None,
    body_rewriter: Callable[[str, bytes], bytes] | None = None,
) -> tuple[ThreadingHTTPServer, TokenCache, httpx.Client]:
    """Start a per-session proxy for agents that only hold a static credential.

    The agent sends ``session_secret`` as its API key; the proxy accepts it only on
    ``allowed_path_prefixes`` and forwards with a fresh Databricks token plus
    ``added_headers``, after removing ``dropped_chat_completions_fields`` from Chat
    Completions request bodies. ``path_rewriter`` maps an accepted request path to the
    gateway path it is forwarded to, and ``body_rewriter(path, body)`` adjusts a request
    body for the gateway. Binds an OS-assigned port, read from ``server.server_address[1]``.
    """
    return _start_proxy(
        workspace,
        token_provider,
        0,
        upstream_path="",
        token_header=AUTHORIZATION_HEADER,
        handler_type=_GatewayAuthProxyHandler,
        force_refresh_near_expiry=True,
        fallback_on_bind_error=False,
        handler_attrs={
            "session_secret": session_secret,
            "added_headers": dict(added_headers),
            "allowed_path_prefixes": allowed_path_prefixes,
            "sign_in_hint": sign_in_hint,
            "dropped_chat_completions_fields": frozenset(dropped_chat_completions_fields),
            # staticmethod: a plain function on the handler class would bind as a method.
            "path_rewriter": staticmethod(path_rewriter) if path_rewriter else None,
            "body_rewriter": staticmethod(body_rewriter) if body_rewriter else None,
        },
        on_refresh_failure=on_refresh_failure,
    )
