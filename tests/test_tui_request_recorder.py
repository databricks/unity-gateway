import gzip
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
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
from tests.integration.utils.harness import UserSession

_MESSAGES_PATH = "/ai-gateway/anthropic/v1/messages"
_PRIVATE = "PRIVATE-prompt-tool-name-argument-model-header-error"


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


def test_rejected_inference_reports_tool_setup_without_conversation():
    payload = {
        "model": "glm",
        "system": "secret system",
        "messages": [{"role": "user", "content": "secret prompt"}],
        "tool_choice": {"type": "auto"},
        "tools": [
            {"name": "Read", "input_schema": {"type": "object"}},
            {"name": "SubagentHandback", "defer_loading": True, "description": "secret"},
        ],
    }
    request = RecordedRequest(1, "POST", "/messages", {}, json.dumps(payload).encode())

    class Rejected:
        def response_for(self, request, timeout):
            return RecordedResponse(400, {}, b'{"error_code": "BAD_REQUEST"}')

    with pytest.raises(AssertionError) as rejected:
        request_recorder.assert_served(Rejected(), request, "glm")

    details = rejected.value.args[0]
    assert details["tool_choice"] == {"type": "auto"}
    assert details["tools"] == [
        {"name": "Read", "defer_loading": None},
        {"name": "SubagentHandback", "defer_loading": True},
    ]
    assert "secret" not in str(details)


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


@contextmanager
def _protocol_upstream(responses, release=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            status, headers, body = responses.pop(0)
            if release is not None:
                assert release.wait(10)
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}"
        finally:
            if release is not None:
                release.set()
            server.shutdown()
            thread.join()


def _message(block_type="text", stop_reason="end_turn"):
    return {
        "type": "message",
        "model": _PRIVATE,
        "content": [
            {
                "type": block_type,
                "text": f"<tool_call>Agent({_PRIVATE})",
                "name": _PRIVATE,
                "input": {"value": _PRIVATE},
                "id": _PRIVATE,
            },
        ],
        "stop_reason": stop_reason,
    }


def _sse(block_type="tool_use", stop_reason="tool_use"):
    events = [
        {"type": "message_start", "message": {**_message(), "content": [], "stop_reason": None}},
        {"type": "content_block_start", "content_block": _message(block_type)["content"][0]},
        {"type": "content_block_delta", "delta": {"type": _PRIVATE, "text": _PRIVATE}},
        {"type": "message_delta", "delta": {"stop_reason": stop_reason}},
        {"type": "message_stop"},
    ]
    return b"".join(
        f": comment {_PRIVATE}\r\nevent: {event['type']}\r\ndata: {{\r\ndata: {json.dumps(event)[1:]}\r\n\r\n".encode()
        for event in events
    )


@pytest.mark.parametrize(
    "body,media_type,encoding,status,state,types,reasons",
    [
        (
            json.dumps(_message()).encode(),
            "application/json",
            None,
            200,
            "complete",
            ["text"],
            ["end_turn"],
        ),
        (json.dumps(_message()).encode(), None, None, 200, "complete", ["text"], ["end_turn"]),
        (
            json.dumps(_message()).encode(),
            "text/plain",
            None,
            200,
            "complete",
            ["text"],
            ["end_turn"],
        ),
        (
            json.dumps(_message("tool_use", "tool_use")).encode(),
            "application/json",
            None,
            200,
            "complete",
            ["tool_use"],
            ["tool_use"],
        ),
        (
            json.dumps(_message("server_tool_use")).encode(),
            "application/json",
            None,
            200,
            "complete",
            ["server_tool_use"],
            ["end_turn"],
        ),
        (
            json.dumps({**_message(), "content": _message()["content"] * 2}).encode(),
            "application/json",
            None,
            200,
            "complete",
            ["text"],
            ["end_turn"],
        ),
        (
            gzip.compress(json.dumps(_message()).encode()),
            "application/json",
            "gzip",
            200,
            "complete",
            ["text"],
            ["end_turn"],
        ),
        (
            _sse(),
            "text/event-stream; charset=utf-8",
            None,
            200,
            "complete",
            ["tool_use"],
            ["tool_use"],
        ),
        (
            _sse("text", "end_turn"),
            "text/event-stream",
            None,
            200,
            "complete",
            ["text"],
            ["end_turn"],
        ),
        (
            gzip.compress(_sse()),
            "text/event-stream",
            "gzip",
            200,
            "complete",
            ["tool_use"],
            ["tool_use"],
        ),
        (
            _sse() + b"\n",
            "text/event-stream",
            None,
            200,
            "complete",
            ["tool_use"],
            ["tool_use"],
        ),
        (
            _sse() + b"\n : whitespace\n: unfinished comment\nevent: ping",
            "text/event-stream",
            None,
            200,
            "complete",
            ["tool_use"],
            ["tool_use"],
        ),
        (
            _sse() + b'data: {"type": "ping"}',
            "text/event-stream",
            None,
            200,
            "incomplete",
            ["tool_use"],
            ["tool_use"],
        ),
        (_sse() + _sse(), "text/event-stream", None, 200, "malformed", ["tool_use"], ["tool_use"]),
        (
            b'data: {"type": "message_stop"}\n\n' + _sse(),
            "text/event-stream",
            None,
            200,
            "malformed",
            [],
            [],
        ),
        (
            _sse().removesuffix(b"\r\n\r\n"),
            "text/event-stream",
            None,
            200,
            "incomplete",
            ["tool_use"],
            ["tool_use"],
        ),
        (
            _sse() + b"data: {broken}\n\n",
            "text/event-stream",
            None,
            200,
            "malformed",
            ["tool_use"],
            ["tool_use"],
        ),
        (
            _sse() + f'data: {{"type": "{_PRIVATE}"}}\n\n'.encode(),
            "text/event-stream",
            None,
            200,
            "unknown",
            ["tool_use"],
            ["tool_use"],
        ),
        (
            f'data: {{"type": "error", "error": "{_PRIVATE}"}}\n\n'.encode(),
            "text/event-stream",
            None,
            200,
            "error",
            [],
            [],
        ),
        (
            b'event: message_stop\ndata: {"type": "message_start"}\n\n',
            "text/event-stream",
            None,
            200,
            "malformed",
            [],
            [],
        ),
        (
            json.dumps(_message(_PRIVATE, _PRIVATE)).encode(),
            "application/json",
            None,
            200,
            "complete",
            ["unknown"],
            ["unknown"],
        ),
        (
            json.dumps({**_message(), "stop_reason": {_PRIVATE: _PRIVATE}}).encode(),
            "application/json",
            None,
            200,
            "malformed",
            ["text"],
            [],
        ),
        (
            json.dumps({**_message(), "content": [_message()["content"][0], _PRIVATE]}).encode(),
            "application/json",
            None,
            200,
            "malformed",
            ["text"],
            [],
        ),
        (
            json.dumps(_message(stop_reason=None)).encode(),
            "application/json",
            None,
            200,
            "incomplete",
            ["text"],
            [],
        ),
        (
            json.dumps({"type": "error", "error": _PRIVATE}).encode(),
            "application/json",
            None,
            400,
            "error",
            [],
            [],
        ),
        (
            f'{{"{_PRIVATE}": "{_PRIVATE}"}}'.encode(),
            "application/json",
            None,
            200,
            "malformed",
            [],
            [],
        ),
        (b"[]", "application/json", None, 200, "malformed", [], []),
        (_PRIVATE.encode(), "application/json", None, 502, "malformed", [], []),
        (b"\xff", "text/event-stream", None, 200, "decode_error", [], []),
        (b"bad gzip", "application/json", "gzip", 200, "decode_error", [], []),
        (_PRIVATE.encode(), "text/html", None, 502, "malformed", [], []),
    ],
)
def test_claude_protocol_projects_only_safe_response_metadata(
    body, media_type, encoding, status, state, types, reasons
):
    headers = {"X-Private": _PRIVATE}
    if media_type is not None:
        headers["Content-Type"] = media_type
    if encoding:
        headers["Content-Encoding"] = encoding
    with _protocol_upstream([(status, headers, body)]) as upstream:
        with request_recorder.TuiRequestRecorder(upstream) as recorder:
            # Consume raw bytes so malformed compression is tested by the snapshot alone.
            with httpx.stream(
                "POST", recorder.url + _MESSAGES_PATH, json={"prompt": _PRIVATE}
            ) as response:
                list(response.iter_raw())
            request = recorder.expect_request(path=_MESSAGES_PATH)
            recorder.response_for(request, timeout=5)
            rows = recorder._claude_response_protocol()
    assert rows == [
        {
            "sequence": 1,
            "status_code": status,
            "state": state,
            "content_block_types": types,
            "stop_reasons": reasons,
        }
    ]
    assert _PRIVATE not in json.dumps(rows)


def test_claude_protocol_keeps_history_and_pending_without_waiting():
    response = (200, {"Content-Type": "application/json"}, json.dumps(_message()).encode())
    release = threading.Event()
    release.set()
    with _protocol_upstream([response] * 4, release) as upstream:
        with request_recorder.TuiRequestRecorder(upstream) as recorder:
            httpx.get(recorder.url + _MESSAGES_PATH)
            httpx.post(recorder.url + "/other")
            httpx.post(recorder.url + _MESSAGES_PATH)
            request = recorder.expect_request(method="POST", path=_MESSAGES_PATH)
            recorder.response_for(request, timeout=5)
            recorder.checkpoint()
            release.clear()
            with ThreadPoolExecutor() as pool:
                pending = pool.submit(httpx.post, recorder.url + _MESSAGES_PATH, timeout=10)
                request = recorder.expect_request(path=_MESSAGES_PATH, after=3, timeout=5)
                try:
                    snapshot = pool.submit(recorder._claude_response_protocol).result(timeout=2)
                    assert snapshot == [
                        {
                            "sequence": 3,
                            "status_code": 200,
                            "state": "complete",
                            "content_block_types": ["text"],
                            "stop_reasons": ["end_turn"],
                        },
                        {
                            "sequence": 4,
                            "status_code": None,
                            "state": "pending",
                            "content_block_types": [],
                            "stop_reasons": [],
                        },
                    ]
                finally:
                    release.set()
                pending.result(timeout=5)


@pytest.fixture
def protocol_session(tmp_path):
    return UserSession(tmp_path / "session", tmp_path, Path(sys.executable), tmp_path / "artifacts")


@pytest.mark.parametrize("fail", [False, True])
def test_claude_protocol_context_records_history_after_exit(protocol_session, fail):
    response = (200, {"Content-Type": "application/json"}, json.dumps(_message()).encode())
    failure = AssertionError("calculation failed before response assertions")
    with _protocol_upstream([response] * 2) as upstream:
        try:
            with request_recorder.record_claude_response_protocol(
                protocol_session, upstream
            ) as recorder:
                for _ in range(2):
                    httpx.post(recorder.url + _MESSAGES_PATH)
                    recorder.checkpoint()
                if fail:
                    raise failure
        except AssertionError as error:
            assert fail and error is failure
    assert recorder._client.is_closed
    rows = json.loads((protocol_session.artifacts / "claude-response-protocol.json").read_text())
    assert [row["sequence"] for row in rows] == [1, 2]
    assert all(row["state"] == "complete" for row in rows)


@pytest.mark.parametrize("outlives_drain", [False, True])
def test_claude_protocol_context_snapshots_after_existing_drain(
    protocol_session, monkeypatch, outlives_drain
):
    response = (200, {"Content-Type": "application/json"}, json.dumps(_message()).encode())
    release = threading.Event()
    timer = threading.Timer(1, release.set)
    if outlives_drain:
        monkeypatch.setattr(request_recorder, "_DRAIN_SECONDS", 0.02)
    with _protocol_upstream([response], release) as upstream, ThreadPoolExecutor() as pool:
        try:
            with request_recorder.record_claude_response_protocol(
                protocol_session, upstream
            ) as recorder:
                pending = pool.submit(httpx.post, recorder.url + _MESSAGES_PATH, timeout=10)
                recorder.expect_request(path=_MESSAGES_PATH, timeout=5)
                if not outlives_drain:
                    timer.start()
            rows = json.loads(
                (protocol_session.artifacts / "claude-response-protocol.json").read_text()
            )
            assert rows == [
                {
                    "sequence": 1,
                    "status_code": None if outlives_drain else 200,
                    "state": "pending" if outlives_drain else "complete",
                    "content_block_types": [] if outlives_drain else ["text"],
                    "stop_reasons": [] if outlives_drain else ["end_turn"],
                }
            ]
        finally:
            release.set()
            timer.cancel()
        pending.result(timeout=5)


def test_claude_protocol_context_records_early_failure(protocol_session):
    failure = AssertionError("configure failed")
    with pytest.raises(AssertionError) as caught:
        with request_recorder.record_claude_response_protocol(
            protocol_session, "http://127.0.0.1:1"
        ):
            raise failure
    assert caught.value is failure
    assert (
        json.loads((protocol_session.artifacts / "claude-response-protocol.json").read_text()) == []
    )


def test_claude_protocol_context_preserves_failure_when_record_write_fails(protocol_session):
    path = protocol_session.artifacts / "claude-response-protocol.json"
    path.mkdir()
    failure = AssertionError("original calculation failure")
    with pytest.raises(AssertionError) as caught:
        with request_recorder.record_claude_response_protocol(
            protocol_session, "http://127.0.0.1:1"
        ):
            raise failure
    assert caught.value is failure
    assert failure.__notes__ == ["Claude response protocol evidence could not be recorded."]
    assert caught.tb.tb_next is None
    assert (
        caught.tb.tb_frame.f_code.co_name
        == "test_claude_protocol_context_preserves_failure_when_record_write_fails"
    )


def test_claude_protocol_context_does_not_suppress_write_failure_in_ambient_except(
    protocol_session,
):
    (protocol_session.artifacts / "claude-response-protocol.json").mkdir()
    try:
        raise AssertionError("unrelated handled failure")
    except AssertionError as ambient:
        with pytest.raises(OSError):
            with request_recorder.record_claude_response_protocol(
                protocol_session, "http://127.0.0.1:1"
            ):
                pass
        assert not getattr(ambient, "__notes__", [])


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
