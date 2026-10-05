"""Record HTTP requests made by a TUI while forwarding them to the real workspace."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlsplit

import httpx

_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "cookie",
        "proxy-authorization",
        "set-cookie",
        "x-api-key",
        "x-databricks-ai-gateway-token",
    }
)
_MAX_REQUEST_BYTES = 16 * 1024 * 1024
_MAX_RECORDED_RESPONSE_BYTES = 1024 * 1024


@dataclass
class RecordedRequest:
    """One request observed by :class:`TuiRequestRecorder`."""

    sequence: int
    method: str
    path: str
    query: str
    headers: Mapping[str, str]
    body: bytes
    response_status: int | None = None
    response_headers: Mapping[str, str] = field(default_factory=dict)
    response_body: bytes = b""
    response_truncated: bool = False
    response_complete: bool = False

    def json(self) -> Any:
        """Decode the request body as JSON."""
        return json.loads(self.body)

    def response_json(self) -> Any:
        """Decode the recorded response body as JSON."""
        if not self.response_complete:
            raise RuntimeError("The upstream response has not completed.")
        if self.response_truncated:
            raise RuntimeError("The upstream response was too large to record completely.")
        return json.loads(self.response_body)


class _RecorderServer(ThreadingHTTPServer):
    daemon_threads = True


class TuiRequestRecorder:
    """A per-test recording reverse proxy for live TUI API requests.

    Point ``ug configure --workspace`` at :attr:`url`. Requests retain their real
    paths and payloads and are forwarded to ``upstream``. Each instance binds an
    OS-assigned loopback port and owns all of its state, so instances are safe to
    use from parallel test processes.
    """

    def __init__(self, upstream: str, *, timeout: float = 30.0) -> None:
        parsed = urlsplit(upstream)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("upstream must be an absolute HTTP(S) URL")
        if parsed.path.rstrip("/") or parsed.query or parsed.fragment:
            raise ValueError("upstream must not contain a path, query, or fragment")

        self.upstream = upstream.rstrip("/")
        self.timeout = timeout
        self._condition = threading.Condition()
        self._requests: list[RecordedRequest] = []
        self._errors: list[str] = []
        self._server: _RecorderServer | None = None
        self._server_thread: threading.Thread | None = None
        self._client: httpx.Client | None = None

    @property
    def url(self) -> str:
        """Return the loopback URL after the recorder has started."""
        if self._server is None:
            raise RuntimeError("TuiRequestRecorder has not been started.")
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> Self:
        if self._server is not None:
            raise RuntimeError("TuiRequestRecorder is already running.")

        self._client = httpx.Client(
            timeout=httpx.Timeout(connect=self.timeout, read=None, write=self.timeout, pool=5.0),
            follow_redirects=False,
        )
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def handle_request(self) -> None:
                recorder._handle(self)

            do_DELETE = handle_request
            do_GET = handle_request
            do_HEAD = handle_request
            do_OPTIONS = handle_request
            do_PATCH = handle_request
            do_POST = handle_request
            do_PUT = handle_request

        try:
            self._server = _RecorderServer(("127.0.0.1", 0), Handler)
            self._server_thread = threading.Thread(
                target=self._server.serve_forever,
                name="tui-request-recorder",
                daemon=True,
            )
            self._server_thread.start()
        except BaseException:
            self._client.close()
            self._client = None
            self._server = None
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        server, server_thread, client = self._server, self._server_thread, self._client
        self._server = None
        self._server_thread = None
        self._client = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if server_thread is not None:
            server_thread.join(timeout=5)
        if client is not None:
            client.close()
        if exc_type is None:
            self._raise_forwarding_error()

    def checkpoint(self) -> int:
        """Return a sequence marker for requests made after this point."""
        with self._condition:
            return self._requests[-1].sequence if self._requests else 0

    def requests(
        self,
        *,
        method: str | None = None,
        path: str | None = None,
        after: int = 0,
    ) -> list[RecordedRequest]:
        """Return a snapshot of matching requests after ``after``."""
        self._raise_forwarding_error()
        normalized_method = method.upper() if method else None
        with self._condition:
            return [
                request
                for request in self._requests
                if request.sequence > after
                and (normalized_method is None or request.method == normalized_method)
                and (path is None or request.path == path)
            ]

    def expect_request(
        self,
        *,
        method: str | None = None,
        path: str | None = None,
        after: int = 0,
        timeout: float | None = None,
    ) -> RecordedRequest:
        """Wait for and return the first matching request."""
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        normalized_method = method.upper() if method else None
        with self._condition:
            while True:
                self._raise_forwarding_error_locked()
                for request in self._requests:
                    if (
                        request.sequence > after
                        and (normalized_method is None or request.method == normalized_method)
                        and (path is None or request.path == path)
                    ):
                        return request
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    description = f"{normalized_method or '*'} {path or '*'}"
                    raise AssertionError(f"Timed out waiting for TUI request: {description}")
                self._condition.wait(remaining)

    def expect_response(
        self, request: RecordedRequest, *, timeout: float | None = None
    ) -> RecordedRequest:
        """Wait until the upstream response for ``request`` has completed."""
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        with self._condition:
            while not request.response_complete:
                self._raise_forwarding_error_locked()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(
                        f"Timed out waiting for response to request {request.sequence}"
                    )
                self._condition.wait(remaining)
            return request

    def assert_no_request(
        self,
        *,
        method: str | None = None,
        path: str | None = None,
        after: int = 0,
    ) -> None:
        """Assert no matching request exists after a completed TUI action."""
        matches = self.requests(method=method, path=path, after=after)
        assert not matches, (
            f"Unexpected TUI request: {matches[0].method} {matches[0].path} "
            f"(sequence {matches[0].sequence})"
        )

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        client = self._client
        if client is None:
            handler.send_error(503, "request recorder is stopping")
            return

        try:
            length = int(handler.headers.get("Content-Length", "0"))
            if length < 0 or length > _MAX_REQUEST_BYTES:
                handler.send_error(413, "request body is too large to record")
                return
            body = handler.rfile.read(length) if length else b""
            parsed = urlsplit(handler.path)
            record = self._record_request(handler, parsed.path, parsed.query, body)
            headers = {
                name: value
                for name, value in handler.headers.items()
                if name.lower() not in _HOP_BY_HOP_HEADERS | {"content-length", "host"}
            }
            upstream_url = f"{self.upstream}{handler.path}"
            with client.stream(
                handler.command, upstream_url, headers=headers, content=body
            ) as response:
                self._relay_response(handler, record, response)
        except (BrokenPipeError, ConnectionResetError):
            # Agents routinely close an SSE connection once their turn is done.
            # The request was still observed and forwarded successfully.
            return
        except (OSError, ValueError, httpx.HTTPError) as error:
            self._record_error(error)
            try:
                handler.send_error(502, "upstream request failed")
            except OSError:
                pass

    def _record_request(
        self,
        handler: BaseHTTPRequestHandler,
        path: str,
        query: str,
        body: bytes,
    ) -> RecordedRequest:
        headers = {
            name.lower(): "<redacted>" if name.lower() in _SENSITIVE_HEADERS else value
            for name, value in handler.headers.items()
        }
        with self._condition:
            record = RecordedRequest(
                sequence=len(self._requests) + 1,
                method=handler.command,
                path=path,
                query=query,
                headers=headers,
                body=body,
            )
            self._requests.append(record)
            self._condition.notify_all()
            return record

    def _relay_response(
        self,
        handler: BaseHTTPRequestHandler,
        record: RecordedRequest,
        response: httpx.Response,
    ) -> None:
        response_headers = {
            name.lower(): "<redacted>" if name.lower() in _SENSITIVE_HEADERS else value
            for name, value in response.headers.items()
        }
        with self._condition:
            record.response_status = response.status_code
            record.response_headers = response_headers
            self._condition.notify_all()

        handler.send_response(response.status_code)
        for name, value in response.headers.items():
            if name.lower() not in _HOP_BY_HOP_HEADERS:
                handler.send_header(name, value)
        handler.end_headers()

        captured = bytearray()
        disconnected = False
        for chunk in response.iter_raw():
            if len(captured) < _MAX_RECORDED_RESPONSE_BYTES:
                remaining = _MAX_RECORDED_RESPONSE_BYTES - len(captured)
                captured.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    record.response_truncated = True
            elif chunk:
                record.response_truncated = True
            if disconnected or not chunk:
                continue
            try:
                if handler.command != "HEAD":
                    handler.wfile.write(chunk)
                    handler.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                disconnected = True

        with self._condition:
            record.response_body = bytes(captured)
            record.response_complete = True
            self._condition.notify_all()

    def _record_error(self, error: BaseException) -> None:
        with self._condition:
            self._errors.append(f"{type(error).__name__}: {error}")
            self._condition.notify_all()

    def _raise_forwarding_error(self) -> None:
        with self._condition:
            self._raise_forwarding_error_locked()

    def _raise_forwarding_error_locked(self) -> None:
        if self._errors:
            raise AssertionError(f"TUI request forwarding failed: {self._errors[0]}")
