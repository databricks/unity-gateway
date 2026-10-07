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

Security invariants (mirroring `databricks.py` token handling):
  - Binds 127.0.0.1 only; never exposed off-host.
  - Never logs header values or bodies. The Databricks token lives in memory,
    refreshed off the request path; the Anthropic OAuth in `Authorization` is
    passed through untouched in relayed mode and never logged.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import sys
import threading
import time
import uuid
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast
from urllib.parse import urlsplit

import httpx

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
_DIAGNOSTICS_TRUE = frozenset({"1", "true", "yes", "on"})
_CODEX_V2_RESPONSE_PATHS = frozenset({"/v1/responses", "/v1/responses/compact"})
_CODEX_V2_MAX_SSE_EVENT_BYTES = 16 * 1024 * 1024
_CODEX_V2_RESPONSE_DROP_HEADERS = frozenset({"content-encoding", "content-length"})


def _diagnostics_enabled() -> bool:
    return os.environ.get(_DIAGNOSTICS_ENV, "").strip().lower() in _DIAGNOSTICS_TRUE


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
    ) -> None:
        # token_provider(force_refresh) mints a fresh token from the same source the
        # client agent uses, so the proxy authenticates as the same principal (e.g.
        # a per-user custom-OAuth token, not the default CLI profile).
        self._token_provider = token_provider
        self._force_refresh_near_expiry = force_refresh_near_expiry
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
                log_token_refresh_failure(exc)

    @property
    def token(self) -> str:
        self._ensure_fresh()
        with self._state_lock:
            return self._token

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
                log_token_refresh_failure(exc)

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
        """Prepare a request body before forwarding it.

        The base relay is intentionally byte-transparent. Specialized handlers may
        override this hook for a narrowly scoped protocol adapter.
        """
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

    def _handle(self) -> None:
        diagnostic_id = uuid.uuid4().hex[:12]
        started = time.monotonic()
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else None
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
            return forwarded_request_headers(
                self, self.cache.token, token_header, extra_strip=extra_strip
            )

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
                log_token_refresh_failure(exc)
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
        except _CodexV2ProtocolError as exc:
            # Headers may already be committed for a streamed response, so the
            # only safe response is a truncated stream with a sanitized diagnostic.
            log_proxy_diagnostic(
                "response_protocol_error",
                request_id=diagnostic_id,
                error_type=type(exc).__name__,
            )

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


def _codex_v2_path(path: str) -> bool:
    return urlsplit(path).path in _CODEX_V2_RESPONSE_PATHS


def _sse_separator(buffer: bytearray) -> tuple[int, int] | None:
    candidates = [
        (index, size)
        for delimiter, size in ((b"\r\n\r\n", 4), (b"\n\n", 2))
        if (index := buffer.find(delimiter)) >= 0
    ]
    return min(candidates) if candidates else None


def _split_sse_frame(frame: bytes) -> tuple[bytes, bytes]:
    for delimiter in (b"\r\n\r\n", b"\n\n"):
        if frame.endswith(delimiter):
            return frame[: -len(delimiter)], delimiter
    raise _CodexV2ProtocolError("Codex v2 SSE frame is incomplete")


def _replace_sse_data(frame: bytes, payload: bytes) -> bytes:
    body, delimiter = _split_sse_frame(frame)
    lines = body.splitlines(keepends=True)
    replaced = False
    output: list[bytes] = []
    for line in lines:
        content = line.rstrip(b"\r\n")
        if content.startswith(b"data:"):
            if not replaced:
                ending = (
                    b"\r\n" if line.endswith(b"\r\n") else b"\n" if line.endswith(b"\n") else b""
                )
                output.append(b"data: " + payload + ending)
                replaced = True
            continue
        output.append(line)
    if not replaced:
        raise _CodexV2ProtocolError("Codex v2 SSE event has no data field")
    return b"".join(output) + delimiter


class _CodexV2ProxyHandler(_ProxyHandler):
    """Loopback adapter for native Codex v2 Responses traffic."""

    def _prepare_request_body(self, body: bytes | None) -> bytes | None:
        self._codex_v2_adapter_active = False
        if self.command.upper() != "POST" or not _codex_v2_path(self.path) or body is None:
            return body
        transformed, changed = prepare_request(body)
        self._codex_v2_adapter_active = changed
        return transformed

    def _forward_target(self, body: bytes | None) -> tuple[str, frozenset[str], str]:
        # Native Codex already authenticates with the Databricks bearer. Replace
        # it from the proxy's TokenCache and remove the alternate swap header so
        # two credentials can never compete on the same upstream request.
        return (
            AUTHORIZATION_HEADER,
            frozenset({AI_GATEWAY_TOKEN_HEADER.lower()}),
            "codex-v2",
        )

    def _response_headers(self, resp: httpx.Response) -> tuple[tuple[str, str], ...]:
        content_type = next(
            (value for key, value in resp.headers.items() if key.lower() == "content-type"),
            "",
        ).lower()
        drop_decoding_headers = (
            getattr(self, "_codex_v2_adapter_active", False)
            and 200 <= resp.status_code < 300
            and ("json" in content_type or "text/event-stream" in content_type)
        )
        return tuple(
            (key, value)
            for key, value in resp.headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS
            and (not drop_decoding_headers or key.lower() not in _CODEX_V2_RESPONSE_DROP_HEADERS)
        )

    def _iter_response_chunks(self, resp: httpx.Response):
        if not getattr(self, "_codex_v2_adapter_active", False) or not (
            200 <= resp.status_code < 300
        ):
            yield from resp.iter_raw()
            return
        content_type = next(
            (value for key, value in resp.headers.items() if key.lower() == "content-type"),
            "",
        ).lower()
        if "text/event-stream" in content_type:
            yield from self._iter_sse_chunks(resp)
            return
        if "json" not in content_type:
            # Error pages and other non-JSON gateway responses are outside the
            # adapter contract and must remain byte-transparent.
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
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _CodexV2ProtocolError("Codex v2 JSON response is malformed") from exc
        if not isinstance(event, dict):
            raise _CodexV2ProtocolError("Codex v2 JSON response is not an object")
        try:
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
            if not chunk:
                continue
            buffer.extend(chunk)
            while separator := _sse_separator(buffer):
                index, delimiter_size = separator
                frame_size = index + delimiter_size
                if frame_size > _CODEX_V2_MAX_SSE_EVENT_BYTES:
                    raise _CodexV2ProtocolError("Codex v2 SSE event exceeds the bounded limit")
                frame = bytes(buffer[:frame_size])
                del buffer[:frame_size]
                yield self._transform_sse_frame(frame)
            if len(buffer) > _CODEX_V2_MAX_SSE_EVENT_BYTES:
                raise _CodexV2ProtocolError("Codex v2 SSE event exceeds the bounded limit")
        if buffer:
            # Do not forward an unterminated data event: the client could execute
            # a partial function call. Comments/blank bytes are harmless and retain
            # the normal SSE EOF behavior.
            if buffer.strip(b"\r\n :"):
                raise _CodexV2ProtocolError("Codex v2 SSE stream ended mid-event")
            yield bytes(buffer)

    @staticmethod
    def _transform_sse_frame(frame: bytes) -> bytes:
        body, _delimiter = _split_sse_frame(frame)
        event_type: str | None = None
        data_lines: list[bytes] = []
        for line in body.splitlines():
            if line.startswith(b"event:"):
                try:
                    event_type = line[6:].lstrip().decode("utf-8", "strict")
                except UnicodeDecodeError as exc:
                    raise _CodexV2ProtocolError("Codex v2 SSE event name is malformed") from exc
            elif line.startswith(b"data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines:
            return frame
        payload = b"\n".join(data_lines)
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
        return _replace_sse_data(frame, serialized)


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
) -> tuple[ThreadingHTTPServer, TokenCache, httpx.Client]:
    """Start the loopback refresh proxy + its background token refresher.

    Binds ``port``, falling back to a fresh OS-assigned port when it is already
    in use (e.g. a prior session's proxy that was killed before its teardown ran
    still holds the socket). The caller reads ``server.server_address[1]`` for the
    actual port and points Claude Code at it.

    ``token_provider(force_refresh)`` mints the token from the same source the
    client agent authenticates with.

    Returns (server, cache, client); the caller runs the server (e.g. in a
    thread) and calls shutdown()/cache.stop()/client.close() on exit.
    """
    upstream_base = f"{workspace.rstrip('/')}/{upstream_path.lstrip('/')}"
    cache = TokenCache(token_provider, force_refresh_near_expiry=force_refresh_near_expiry)
    client: httpx.Client | None = None
    server: ThreadingHTTPServer | None = None
    try:
        # One pooled, keep-alive client shared across handler threads: reuses TCP+TLS
        # to the gateway instead of a fresh handshake per request. Don't follow
        # redirects — a proxy relays 3xx verbatim.
        client = httpx.Client(
            base_url=upstream_base, timeout=UPSTREAM_TIMEOUT, follow_redirects=False
        )

        handler = cast(
            type[_ProxyHandler],
            type(
                "BoundProxyHandler",
                (handler_type,),
                {
                    "cache": cache,
                    "client": client,
                    "token_header": token_header,
                },
            ),
        )
        try:
            server = _LoopbackHTTPServer(("127.0.0.1", port), handler)
        except OSError:
            # Cached port is occupied (stale proxy from a killed session). Port 0 lets
            # the OS pick any free port; the caller reconciles the base URL to it.
            server = _LoopbackHTTPServer(("127.0.0.1", 0), handler)

        refresher = threading.Thread(target=cache.run_refresher, daemon=True)
        refresher.start()
        return server, cache, client
    except BaseException:
        cache.stop()
        if server is not None:
            server.server_close()
        if client is not None:
            client.close()
        raise


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
) -> tuple[ThreadingHTTPServer, TokenCache, httpx.Client]:
    """Start the Codex OTLP proxy on an OS-assigned port."""
    return _start_proxy(
        workspace,
        token_provider,
        0,
        upstream_path="ai-gateway/otel/",
        token_header=AUTHORIZATION_HEADER,
        handler_type=_ProxyHandler,
        force_refresh_near_expiry=True,
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
