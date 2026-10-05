"""Record TUI HTTP requests while forwarding them to a real workspace."""

import json
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlsplit

import httpx

_STRIPPED_HEADERS = {
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
_SECRET_HEADERS = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "set-cookie",
    "x-api-key",
    "x-databricks-ai-gateway-token",
}


@dataclass(frozen=True)
class RecordedRequest:
    sequence: int
    method: str
    path: str
    headers: dict[str, str]
    body: bytes

    @property
    def payload(self) -> Any:
        """The JSON request payload."""
        return json.loads(self.body)


@dataclass(frozen=True)
class RecordedResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes

    @property
    def payload(self) -> Any:
        """The JSON response payload."""
        return json.loads(self.body)


class _Server(ThreadingHTTPServer):
    daemon_threads = True


class TuiRequestRecorder:
    """A per-test recording reverse proxy for requests made by a TUI."""

    def __init__(self, upstream: str) -> None:
        parsed = urlsplit(upstream)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("upstream must be an absolute HTTP(S) URL")
        self.upstream = upstream.rstrip("/")
        self._condition = threading.Condition()
        self._requests: list[RecordedRequest] = []
        self._responses: dict[int, RecordedResponse] = {}
        self._client: httpx.Client | None = None
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("TuiRequestRecorder is not running")
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> Self:
        self._client = httpx.Client(timeout=None, follow_redirects=False)
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def forward(self) -> None:
                recorder._forward(self)

            do_GET = forward
            do_POST = forward

        self._server = _Server(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        assert self._server and self._thread and self._client
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()
        self._client.close()

    def checkpoint(self) -> int:
        with self._condition:
            return len(self._requests)

    def expect_request(
        self,
        *,
        method: str | None = None,
        path: str | None = None,
        after: int = 0,
        timeout: float = 30,
    ) -> RecordedRequest:
        deadline = time.monotonic() + timeout
        method = method.upper() if method else None
        with self._condition:
            while True:
                for request in self._requests:
                    if (
                        request.sequence > after
                        and (method is None or request.method == method)
                        and (path is None or request.path == path)
                    ):
                        return request
                if (remaining := deadline - time.monotonic()) <= 0:
                    raise AssertionError(f"Timed out waiting for TUI request: {method} {path}")
                self._condition.wait(remaining)

    def expect_response(self, request: RecordedRequest, *, timeout: float = 30) -> RecordedResponse:
        deadline = time.monotonic() + timeout
        with self._condition:
            while request.sequence not in self._responses:
                if (remaining := deadline - time.monotonic()) <= 0:
                    raise AssertionError(
                        f"Timed out waiting for response to request {request.sequence}"
                    )
                self._condition.wait(remaining)
            return self._responses[request.sequence]

    def _forward(self, handler: BaseHTTPRequestHandler) -> None:
        assert self._client
        length = int(handler.headers.get("Content-Length", 0))
        body = handler.rfile.read(length) if length else b""
        parsed = urlsplit(handler.path)
        headers = {
            name.lower(): "<redacted>" if name.lower() in _SECRET_HEADERS else value
            for name, value in handler.headers.items()
        }
        with self._condition:
            request = RecordedRequest(
                sequence=len(self._requests) + 1,
                method=handler.command,
                path=parsed.path,
                headers=headers,
                body=body,
            )
            self._requests.append(request)
            self._condition.notify_all()

        forwarded_headers = {
            name: value
            for name, value in handler.headers.items()
            if name.lower() not in _STRIPPED_HEADERS
        }
        try:
            with self._client.stream(
                handler.command,
                f"{self.upstream}{handler.path}",
                headers=forwarded_headers,
                content=body,
            ) as response:
                handler.send_response(response.status_code)
                for name, value in response.headers.items():
                    if name.lower() not in _STRIPPED_HEADERS:
                        handler.send_header(name, value)
                handler.end_headers()
                response_body = bytearray()
                connected = True
                for chunk in response.iter_raw():
                    response_body.extend(chunk)
                    if connected:
                        try:
                            handler.wfile.write(chunk)
                            handler.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            connected = False
                with self._condition:
                    self._responses[request.sequence] = RecordedResponse(
                        status_code=response.status_code,
                        headers={
                            name.lower(): (
                                "<redacted>" if name.lower() in _SECRET_HEADERS else value
                            )
                            for name, value in response.headers.items()
                        },
                        body=bytes(response_body),
                    )
                    self._condition.notify_all()
        except httpx.HTTPError:
            handler.send_error(502, "upstream request failed")
