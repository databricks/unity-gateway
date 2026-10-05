import json
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
            body = json.dumps({"selected_model": "system.ai.test"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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

        response = recorder.expect_response(request)
        assert response.status_code == 200
        assert response.payload["selected_model"] == "system.ai.test"
