"""Component coverage for optional parent-launcher startup observations."""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ucode import startup_telemetry


@pytest.fixture
def receiver(monkeypatch):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(0.2)
        context = {
            "version": 1,
            "key": "test-key",
            "launch_id": "test-launch",
            "event_port": sock.getsockname()[1],
        }
        monkeypatch.setenv(startup_telemetry.LAUNCH_CONTEXT_ENV, json.dumps(context))
        yield sock


def receive(sock):
    return json.loads(sock.recv(8192))


def test_startup_clock_survives_dispatch_and_handoff(receiver, monkeypatch):
    """UG records its entry and handoff without replacing the parent's origin."""
    entry = time.monotonic_ns()
    monkeypatch.setenv(startup_telemetry.UG_ENTRY_ENV, str(entry))

    @startup_telemetry.observe_agent_launch
    def launch():
        startup_telemetry.record_dispatch()
        startup_telemetry.record_handoff()
        return "result"

    assert launch() == "result"
    events = [receive(receiver) for _ in range(3)]
    assert [event["event"] for event in events] == ["ug_entry", "ug_dispatch", "ug_handoff"]
    assert events[0]["entry_monotonic_ns"] == entry
    assert all(event["launch_id"] == "test-launch" for event in events)
    assert events[2]["monotonic_ns"] >= events[1]["monotonic_ns"] >= entry


def test_launch_error_is_preserved_without_exporting_error_text(receiver):
    @startup_telemetry.observe_agent_launch
    def launch():
        raise RuntimeError("PRIVATE-ERROR")

    with pytest.raises(RuntimeError, match="PRIVATE-ERROR"):
        launch()
    events = [receive(receiver), receive(receiver)]
    assert events[-1]["event"] == "startup_failed"
    assert events[-1]["component"] == "ug"
    assert "PRIVATE" not in json.dumps(events)


def test_interruption_is_classified_as_cancellation(receiver):
    @startup_telemetry.observe_agent_launch
    def launch():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        launch()
    assert receive(receiver)["event"] == "ug_entry"
    assert receive(receiver)["event"] == "startup_cancelled"


def test_successful_process_exit_is_not_a_launch_error(receiver):
    @startup_telemetry.observe_agent_launch
    def launch():
        raise SystemExit(0)

    with pytest.raises(SystemExit) as error:
        launch()
    assert error.value.code == 0
    assert receive(receiver)["event"] == "ug_entry"
    with pytest.raises(socket.timeout):
        receive(receiver)


def test_uninstrumented_and_malformed_contexts_leave_launch_unchanged(monkeypatch, capsys):
    @startup_telemetry.observe_agent_launch
    def launch():
        return 37

    for context in ("", "[]", "invalid", '{"version":2}', '{"version":1,"event_port":true}'):
        monkeypatch.setenv(startup_telemetry.LAUNCH_CONTEXT_ENV, context)
        assert launch() == 37
    assert capsys.readouterr() == ("", "")


def test_invalid_entry_clock_cannot_break_a_launch(receiver, monkeypatch):
    monkeypatch.setenv(startup_telemetry.UG_ENTRY_ENV, "9" * 10000)

    @startup_telemetry.observe_agent_launch
    def launch():
        return 9

    assert launch() == 9
    assert receive(receiver)["event"] == "ug_entry"


def test_receiver_failure_preserves_existing_trace_settings(receiver, monkeypatch):
    monkeypatch.setenv("ISAAC_CLI_METRICS_RECEIVER", "[]")
    env = {"CLAUDE_CODE_ENABLE_TELEMETRY": "1", "OTEL_TRACES_EXPORTER": "none"}
    startup_telemetry.observe_claude_metrics(env)
    assert env == {"CLAUDE_CODE_ENABLE_TELEMETRY": "1", "OTEL_TRACES_EXPORTER": "none"}


@pytest.mark.parametrize("original", [None, "https://example.cloud.databricks.com/v1/traces"])
def test_parent_observer_preserves_an_existing_trace_destination(receiver, monkeypatch, original):
    registrations = []

    class Receiver(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            registrations.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            body = json.dumps({"route_id": "b" * 32}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv(
            "ISAAC_CLI_METRICS_RECEIVER",
            json.dumps({"http_port": server.server_port, "key": "a" * 64}),
        )
        env = {
            "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
            "OTEL_TRACES_EXPORTER": "otlp" if original else "none",
        }
        if original:
            env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = original
        startup_telemetry.observe_claude_metrics(env)
        assert registrations == [{"harness": "claude", "signal": "traces", "forward_url": original}]
        assert (
            env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"]
            == f"http://127.0.0.1:{server.server_port}/{'a' * 64}/{'b' * 32}"
        )
        assert env["OTEL_EXPORTER_OTLP_TRACES_PROTOCOL"] == "http/protobuf"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
