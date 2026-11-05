"""Component coverage using real HTTP responses, without workspace credentials."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ucode import databricks


@pytest.fixture
def model_service_endpoint():
    statuses = []
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            status = statuses.pop(0) if len(statuses) > 1 else statuses[0]
            body = json.dumps(
                {"model_services": [{"name": "model-services/system.ai.gpt-5"}]}
                if status == 200
                else {"error_code": "TEST_ERROR", "message": "request refused"}
            ).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", statuses, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 405, 410, 422])
def test_permanent_discovery_error_returns_without_repeating(model_service_endpoint, status):
    workspace, statuses, requests = model_service_endpoint
    statuses.append(status)

    entries, reason = databricks.list_model_service_entries(
        workspace, "test-token", use_cache=False
    )

    assert entries == []
    assert reason.startswith(f"HTTP {status} ")
    assert "request refused" in reason
    assert len(requests) == 1


@pytest.mark.parametrize("status", [408, 409, 425, 429, 499, 500, 502, 503, 504])
def test_transient_discovery_error_can_still_recover(model_service_endpoint, status):
    workspace, statuses, requests = model_service_endpoint
    statuses.extend([status, 200])

    entries, reason = databricks.list_model_service_entries(
        workspace, "test-token", use_cache=False
    )

    assert [entry.id for entry in entries] == ["system.ai.gpt-5"]
    assert reason is None
    assert len(requests) == 2


def test_persistent_transient_discovery_error_remains_bounded(model_service_endpoint):
    workspace, statuses, requests = model_service_endpoint
    statuses.append(503)

    entries, reason = databricks.list_model_service_entries(
        workspace, "test-token", use_cache=False
    )

    assert entries == []
    assert reason.startswith("HTTP 503 ")
    assert len(requests) == 4


def test_permanent_error_after_transient_failure_stops_retries(model_service_endpoint):
    workspace, statuses, requests = model_service_endpoint
    statuses.extend([504, 403])

    entries, reason = databricks.list_model_service_entries(
        workspace, "test-token", use_cache=False
    )

    assert entries == []
    assert reason.startswith("HTTP 403 ")
    assert len(requests) == 2
