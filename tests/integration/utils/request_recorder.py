"""Record TUI HTTP requests while forwarding them to a real workspace."""

from __future__ import annotations

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
# How long teardown waits for in-flight forwards before leaving their connections to them.
_DRAIN_SECONDS = 10


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
        self._in_flight = 0
        self._client: httpx.Client | None = None
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("TuiRequestRecorder is not running")
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> Self:
        # One connection per forwarded request: a pooled keep-alive connection that the gateway
        # or a load balancer closed while idle fails on reuse with "Server disconnected without
        # sending a response". The request is never resent; this only avoids reuse.
        self._client = httpx.Client(
            timeout=None,
            follow_redirects=False,
            limits=httpx.Limits(max_keepalive_connections=0),
        )
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def forward(self) -> None:
                with recorder._condition:
                    recorder._in_flight += 1
                try:
                    recorder._forward(self)
                finally:
                    with recorder._condition:
                        recorder._in_flight -= 1
                        recorder._condition.notify_all()

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
        # Handler threads are daemons, so a forward can still be streaming here. Closing its
        # socket from this thread frees the descriptor while OpenSSL keeps reading the old
        # number, which a later connection in this process may reuse: that thread then reads
        # (and corrupts) the new connection's bytes. Close only once forwards have drained;
        # a forward that outlives the wait closes its own connection when it finishes.
        with self._condition:
            drained = self._condition.wait_for(lambda: self._in_flight == 0, timeout=_DRAIN_SECONDS)
        if drained:
            self._client.close()

    def checkpoint(self) -> int:
        with self._condition:
            return len(self._requests)

    def requests_after(self, checkpoint: int) -> tuple[RecordedRequest, ...]:
        """Return the requests recorded after a session boundary."""
        with self._condition:
            return tuple(request for request in self._requests if request.sequence > checkpoint)

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

    def response_for(self, request: RecordedRequest, *, timeout: float = 30) -> RecordedResponse:
        """Wait for the response sent back for a recorded request."""
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
        except httpx.HTTPError as error:
            # Record the transport failure so a waiter sees the 502 the agent saw
            # instead of timing out for a response that can never arrive.
            with self._condition:
                self._responses.setdefault(
                    request.sequence,
                    RecordedResponse(
                        status_code=502,
                        headers={},
                        body=f"upstream request failed: {type(error).__name__}: {error}".encode(),
                    ),
                )
                self._condition.notify_all()
            handler.send_error(502, "upstream request failed")


def assert_served(recorder, request, model):
    """The request asked for `model` and its paired response was a non-empty HTTP 200."""
    assert request.payload["model"] == model, request.payload
    response = recorder.response_for(request, timeout=240)
    assert response.status_code == 200, {
        "status": response.status_code,
        "model": request.payload["model"],
        "output_config": request.payload.get("output_config"),
        "thinking": request.payload.get("thinking"),
        "response_body": httpx.Response(
            response.status_code, headers=response.headers, content=response.body
        ).text[:2000],
    }
    assert response.body, "Inference response was empty"


def served_inference_request(recorder, requests, request, agent):
    """Require HTTP 200 or verified native Claude compatibility retries."""
    model = request.payload["model"]
    # Claude 2.1.290 may remove display, then safeguards after separate Bedrock
    # validation errors. Inspect recorded traffic only; never replay or edit it.
    # Each field may disappear once, and the full task/model/budget/effort must survive.
    for _ in range(3):
        payload = request.payload
        thinking = payload.get("thinking", {})
        thinking_type = thinking.get("type")
        response = recorder.response_for(request, timeout=240)
        error_message = None
        if agent == "claude" and response.status_code == 400:
            try:
                error = httpx.Response(
                    response.status_code, headers=response.headers, content=response.body
                ).json()
                if isinstance(error, dict) and error.get("error_code") == "BAD_REQUEST":
                    detail = json.loads(error.get("message", ""))
                    if isinstance(detail, dict) and set(detail) == {"message"}:
                        error_message = detail["message"]
            except (ValueError, TypeError):
                pass
        expected_retry = None
        if (
            thinking_type in {"adaptive", "enabled"}
            and thinking.get("display") == "updates"
            and error_message
            == f"thinking.{thinking_type}.display: Input should be 'summarized', 'omitted'"
        ):
            expected_retry = {
                **payload,
                "thinking": {key: value for key, value in thinking.items() if key != "display"},
            }
        elif (
            "safeguards" in payload
            and error_message == "safeguards: Extra inputs are not permitted"
        ):
            expected_retry = {key: value for key, value in payload.items() if key != "safeguards"}
        if expected_retry is None:
            assert_served(recorder, request, model)
            return request
        following = requests[requests.index(request) + 1 :]
        # Parent and child traffic can interleave on the same endpoint.
        retry = next(
            (
                candidate
                for candidate in following
                if candidate.method == request.method
                and candidate.path == request.path
                and candidate.payload.get("system") == payload.get("system")
                and candidate.payload.get("metadata") == payload.get("metadata")
            ),
            None,
        )
        assert retry is not None, "Claude compatibility rejection had no retry"
        assert retry.payload == expected_retry, (
            "Claude compatibility retry changed more than the rejected field",
            error_message,
            retry.payload,
        )
        request = retry
    raise AssertionError("Claude compatibility retries did not reach a successful response")
