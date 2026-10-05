import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from tests.e2e_cuj.helpers.tui_request_recorder import TuiRequestRecorder


@pytest.fixture
def local_endpoint_url():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(204)
            self.end_headers()

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{server.server_address[1]}"
        server.shutdown()
        thread.join()


def test_records_request_payload(local_endpoint_url):
    with TuiRequestRecorder(local_endpoint_url) as recorder:
        httpx.post(
            f"{recorder.url}/ai-gateway/routing/v1/routes:select",
            json={"task": {"prompt": "hello"}},
        )

        request = recorder.expect_request(
            method="POST", path="/ai-gateway/routing/v1/routes:select"
        )
        assert request.payload["task"]["prompt"] == "hello"
