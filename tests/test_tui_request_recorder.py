import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from tests.e2e_cuj.helpers.tui_request_recorder import TuiRequestRecorder


class _Upstream:
    def __init__(self):
        self.requests = []
        self.first_stream_chunk = threading.Event()
        self.release_stream = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args):
                return

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                owner.requests.append((self.path, dict(self.headers), body))
                if self.path == "/stream":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    first = b"data: first\n\n"
                    self.wfile.write(f"{len(first):x}\r\n".encode() + first + b"\r\n")
                    self.wfile.flush()
                    owner.first_stream_chunk.set()
                    owner.release_stream.wait(5)
                    second = b"data: second\n\n"
                    self.wfile.write(f"{len(second):x}\r\n".encode() + second + b"\r\n")
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                    return

                response = json.dumps({"selected_model": "system.ai.test"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.send_header("Set-Cookie", "secret=response")
                self.end_headers()
                self.wfile.write(response)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.release_stream.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def test_records_and_forwards_exact_request():
    with _Upstream() as upstream, TuiRequestRecorder(upstream.url) as recorder:
        checkpoint = recorder.checkpoint()
        payload = {"task": {"prompt": "unique prompt"}, "route_options": ["a", "b"]}
        response = httpx.post(
            f"{recorder.url}/ai-gateway/routing/v1/routes:select?test=true",
            headers={"Authorization": "Bearer secret", "X-Test-Header": "kept"},
            json=payload,
        )

        assert response.json() == {"selected_model": "system.ai.test"}
        request = recorder.expect_request(
            method="POST",
            path="/ai-gateway/routing/v1/routes:select",
            after=checkpoint,
        )
        recorder.expect_response(request)
        assert request.query == "test=true"
        assert request.json() == payload
        assert request.headers["authorization"] == "<redacted>"
        assert request.headers["x-test-header"] == "kept"
        assert request.response_status == 200
        assert request.response_headers["set-cookie"] == "<redacted>"
        assert request.response_json() == {"selected_model": "system.ai.test"}

        path, headers, body = upstream.requests[0]
        assert path == "/ai-gateway/routing/v1/routes:select?test=true"
        assert headers["Authorization"] == "Bearer secret"
        assert headers["X-Test-Header"] == "kept"
        assert headers["Host"] == upstream.url.removeprefix("http://")
        assert json.loads(body) == payload


def test_waits_for_request_and_uses_sequence_checkpoints():
    with _Upstream() as upstream, TuiRequestRecorder(upstream.url) as recorder:
        checkpoint = recorder.checkpoint()

        thread = threading.Thread(
            target=lambda: (
                time.sleep(0.05),
                httpx.post(f"{recorder.url}/later", json={"value": 1}),
            )
        )
        thread.start()
        request = recorder.expect_request(method="POST", path="/later", after=checkpoint)
        thread.join(timeout=5)

        assert request.sequence > checkpoint
        recorder.assert_no_request(path="/missing", after=checkpoint)
        with pytest.raises(AssertionError, match="Unexpected TUI request"):
            recorder.assert_no_request(path="/later", after=checkpoint)


def test_instances_have_isolated_ports_and_records():
    with (
        _Upstream() as upstream,
        TuiRequestRecorder(upstream.url) as first,
        TuiRequestRecorder(upstream.url) as second,
    ):
        assert first.url != second.url
        httpx.post(f"{first.url}/first", json={})
        assert [request.path for request in first.requests()] == ["/first"]
        assert second.requests() == []


def test_streaming_response_is_not_buffered():
    with _Upstream() as upstream, TuiRequestRecorder(upstream.url) as recorder:
        with httpx.stream("POST", f"{recorder.url}/stream", content=b"{}", timeout=5) as response:
            chunks = response.iter_raw()
            first = next(chunks)
            assert upstream.first_stream_chunk.is_set()
            assert b"data: first" in first
            assert not upstream.release_stream.is_set()
            upstream.release_stream.set()
            assert b"data: second" in b"".join(chunks)


@pytest.mark.parametrize(
    "upstream",
    ["not-a-url", "ftp://example.com", "https://example.com/path", "https://example.com?q=1"],
)
def test_rejects_invalid_upstream(upstream):
    with pytest.raises(ValueError):
        TuiRequestRecorder(upstream)
