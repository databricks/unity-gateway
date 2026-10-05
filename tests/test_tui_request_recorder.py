import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from tests.e2e_cuj.helpers.tui_request_recorder import TuiRequestRecorder


class Upstream:
    def __init__(self):
        self.requests = []
        self.release_stream = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                owner.requests.append((self.path, dict(self.headers), body))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"first")
                self.wfile.flush()
                if self.path == "/stream":
                    owner.release_stream.wait(5)
                    self.wfile.write(b"second")

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
        self.thread.join()


def test_records_and_forwards_request():
    with Upstream() as upstream, TuiRequestRecorder(upstream.url) as recorder:
        checkpoint = recorder.checkpoint()
        payload = {"task": {"prompt": "unique prompt"}}
        response = httpx.post(
            f"{recorder.url}/routes:select?test=true",
            headers={"Authorization": "Bearer secret", "X-Test": "kept"},
            json=payload,
        )

        assert response.content == b"first"
        request = recorder.expect_request(path="/routes:select", after=checkpoint)
        assert request.query == "test=true"
        assert request.payload["task"]["prompt"] == "unique prompt"
        assert request.headers["authorization"] == "<redacted>"

        path, headers, body = upstream.requests[0]
        assert path == "/routes:select?test=true"
        assert headers["Authorization"] == "Bearer secret"
        assert headers["X-Test"] == "kept"
        assert json.loads(body) == payload


def test_waits_for_request():
    with Upstream() as upstream, TuiRequestRecorder(upstream.url) as recorder:
        thread = threading.Thread(
            target=lambda: (time.sleep(0.05), httpx.post(f"{recorder.url}/later"))
        )
        thread.start()
        assert recorder.expect_request(path="/later").path == "/later"
        thread.join()


def test_recorders_are_isolated():
    with (
        Upstream() as upstream,
        TuiRequestRecorder(upstream.url) as first,
        TuiRequestRecorder(upstream.url) as second,
    ):
        assert first.url != second.url
        httpx.post(f"{first.url}/first")
        assert [request.path for request in first.requests()] == ["/first"]
        assert second.requests() == []


def test_streams_without_waiting_for_response_to_finish():
    with Upstream() as upstream, TuiRequestRecorder(upstream.url) as recorder:
        with httpx.stream("POST", f"{recorder.url}/stream", timeout=5) as response:
            chunks = response.iter_raw()
            assert next(chunks) == b"first"
            assert not upstream.release_stream.is_set()
            upstream.release_stream.set()
            assert b"".join(chunks) == b"second"


def test_rejects_invalid_upstream():
    with pytest.raises(ValueError):
        TuiRequestRecorder("not-a-url")
