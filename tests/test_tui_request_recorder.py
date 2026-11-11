import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

import tests.e2e_cuj.helpers.tui_request_recorder as recorder_module
from tests.e2e_cuj.helpers.tui_request_recorder import (
    RecordedRequest,
    RecordedResponse,
    TuiRequestRecorder,
)
from tests.integration.utils import request_recorder


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


def _configured_session(recorder, tmp_path, commands):
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
            (app / "refresh_ts.json").write_text(
                json.dumps(
                    {
                        recorder.upstream: {
                            "outcome": "published",
                            "policy_fingerprint": {"exists": False},
                        }
                    }
                )
            )

    return Session()


def test_configures_isolated_session_state_for_recorder(recorder, tmp_path, monkeypatch):
    commands = []
    monkeypatch.setattr(recorder_module, "WORKSPACE_POLICY_PATH", tmp_path / "absent.json")

    recorder.configure_session(
        _configured_session(recorder, tmp_path, commands),
        ["configure", "--disable-databricks-ai-tools"],
    )

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
    refresh = json.loads((tmp_path / ".ucode/refresh_ts.json").read_text())
    assert refresh[recorder.url] == refresh[recorder.upstream]
    # Without an installed policy there is nothing to retarget before a launch.
    recorder.prepare_launch()
    assert json.loads((tmp_path / ".ucode/refresh_ts.json").read_text()) == refresh


@pytest.mark.skipif(sys.platform == "win32", reason="CUJs run on Linux")
def test_retargets_an_installed_policy_to_the_recorder(recorder, tmp_path, monkeypatch):
    policy = tmp_path / "managed-settings.json"
    policy.write_text(json.dumps({"workspace": recorder.upstream, "config": {"spec_version": 1}}))
    monkeypatch.setattr(recorder_module, "WORKSPACE_POLICY_PATH", policy)
    copies = []

    def fake_sudo_cp(command, check):
        assert command[:2] == ["sudo", "cp"] and check is True
        copies.append(command[3])
        policy.write_text(Path(command[2]).read_text())

    monkeypatch.setattr(recorder_module.subprocess, "run", fake_sudo_cp)

    recorder.configure_session(_configured_session(recorder, tmp_path, []), ["configure"])

    assert copies == [str(policy)]
    assert json.loads(policy.read_text())["workspace"] == recorder.url
    entry = json.loads((tmp_path / ".ucode/refresh_ts.json").read_text())[recorder.url]
    assert "last_checked_at" not in entry
    assert entry["policy_fingerprint"]["exists"] is True


@pytest.fixture
def held_upstream():
    """An upstream whose response body stays open until the test releases it."""
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "4")
            self.end_headers()
            self.wfile.write(b"he")
            self.wfile.flush()
            release.wait(10)
            self.wfile.write(b"ld")

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{server.server_address[1]}", release
        release.set()
        server.shutdown()
        thread.join()


def _start_held_request(recorder):
    bodies = []
    thread = threading.Thread(
        target=lambda: bodies.append(httpx.get(f"{recorder.url}/held", timeout=10).content)
    )
    thread.start()
    recorder.expect_request(method="GET", path="/held", timeout=5)
    return thread, bodies


def test_teardown_waits_for_in_flight_forward_before_closing_client(held_upstream):
    endpoint, release = held_upstream
    recorder = TuiRequestRecorder(endpoint).__enter__()
    thread, bodies = _start_held_request(recorder)

    threading.Timer(0.3, release.set).start()
    recorder.__exit__(None, None, None)

    thread.join(5)
    assert bodies == [b"held"]
    assert recorder._client.is_closed


def test_teardown_leaves_client_open_for_forward_that_outlives_drain(held_upstream, monkeypatch):
    # Closing the client under a still-streaming forward frees its socket descriptor while the
    # forward's TLS reader keeps using that number; a reused descriptor then corrupts another
    # connection in the process. The forward must finish on its own connection instead.
    monkeypatch.setattr(request_recorder, "_DRAIN_SECONDS", 0.2)
    endpoint, release = held_upstream
    recorder = TuiRequestRecorder(endpoint).__enter__()
    thread, bodies = _start_held_request(recorder)

    recorder.__exit__(None, None, None)
    assert not recorder._client.is_closed

    release.set()
    thread.join(5)
    assert bodies == [b"held"]
