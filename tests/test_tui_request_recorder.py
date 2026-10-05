import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from tests.e2e_cuj.helpers.tui_request_recorder import (
    RecordedRequest,
    RecordedResponse,
    TuiRequestRecorder,
)


@pytest.fixture
def recorder():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = json.dumps({"selected_model": "system.ai.test"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = f"http://127.0.0.1:{server.server_address[1]}"
        with TuiRequestRecorder(endpoint) as recorder:
            yield recorder
        server.shutdown()
        thread.join()


def assert_request(request: RecordedRequest):
    assert request.payload["task"]["prompt"] == "hello"


def assert_response(response: RecordedResponse):
    assert response.status_code == 200
    assert response.payload["selected_model"] == "system.ai.test"


def test_records_request_and_response(recorder):
    path = "/ai-gateway/routing/v1/routes:select"
    httpx.post(f"{recorder.url}{path}", json={"task": {"prompt": "hello"}})

    request = recorder.expect_request(method="POST", path=path)
    assert_request(request)
    assert_response(recorder.response_for(request))
