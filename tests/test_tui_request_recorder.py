import json
import threading
from datetime import datetime
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
            body = json.dumps({"result": "ok"}).encode()
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
    assert response.payload["result"] == "ok"


def test_records_request_and_response(recorder):
    path = "/test/requests"
    checkpoint = recorder.checkpoint()
    httpx.post(f"{recorder.url}{path}", json={"task": {"prompt": "hello"}})

    request = recorder.expect_request(method="POST", path=path)
    assert recorder.requests_after(checkpoint) == (request,)
    assert_request(request)
    assert_response(recorder.response_for(request))


def test_configures_isolated_session_state_for_recorder(recorder, tmp_path):
    commands = []

    class Session:
        home = tmp_path

        def configure(self, args):
            commands.append(args)
            app = self.home / ".ucode"
            app.mkdir(exist_ok=True)
            (app / "state.json").write_text(
                json.dumps(
                    {
                        "state_version": 3,
                        "current_workspace": recorder.upstream,
                        "workspaces": {
                            recorder.upstream: {
                                "available_tools": ["claude", "codex"],
                                "claude_models": {"haiku": "system.ai.claude-haiku-4-5"},
                            }
                        },
                    }
                )
            )
            (app / "managed-config.json").write_text(
                json.dumps(
                    {
                        "workspace": recorder.upstream,
                        "config": {"spec_version": 1},
                        "retrieved_at": "2000-01-01T00:00:00+00:00",
                        "outcome": "published",
                    }
                )
            )

    recorder.configure_session(Session(), ["configure", "--disable-databricks-ai-tools"])

    assert commands == [
        [
            "configure",
            "--disable-databricks-ai-tools",
            "--workspace",
            recorder.upstream,
        ]
    ]
    state = json.loads((tmp_path / ".ucode/state.json").read_text())
    assert state["current_workspace"] == recorder.url
    assert state["workspaces"][recorder.url] == state["workspaces"][recorder.upstream]
    managed_path = tmp_path / ".ucode/managed-config.json"
    managed = json.loads(managed_path.read_text())
    assert managed["workspace"] == recorder.url
    first_refresh = datetime.fromisoformat(managed["retrieved_at"])

    managed["retrieved_at"] = "2000-01-01T00:00:00+00:00"
    managed_path.write_text(json.dumps(managed))
    recorder.prepare_launch()

    refreshed = json.loads(managed_path.read_text())
    assert datetime.fromisoformat(refreshed["retrieved_at"]) >= first_refresh
