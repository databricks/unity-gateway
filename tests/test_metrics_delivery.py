"""Real processes and HTTPS verify delivery across CLI exit without waiting for uploads."""

from __future__ import annotations

import ipaddress
import json
import os
import queue
import ssl
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest


@pytest.fixture
def localhost_certificate(tmp_path):
    # Generate disposable TLS material for the local receiver; never commit a private key.
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        # The OS trust store (ug's default) applies the platform's TLS policy; macOS requires this.
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    path = tmp_path / "metrics-localhost.pem"
    path.write_bytes(
        certificate.public_bytes(serialization.Encoding.PEM)
        + key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return path


@pytest.mark.parametrize("handoff", [False, True], ids=["command-exit", "agent-launch"])
def test_short_command_delivers_final_metric_after_parent_exits(
    tmp_path, handoff, localhost_certificate
):
    received = queue.Queue()
    respond = threading.Event()
    certificate = localhost_certificate

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            request = ExportMetricsServiceRequest.FromString(
                self.rfile.read(int(self.headers["Content-Length"]))
            )
            received.put((self.path, self.headers["Authorization"], request))
            # The CLI must finish even while its detached uploader is waiting for a response.
            respond.wait(10)
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate)
    with ThreadingHTTPServer(("127.0.0.1", 0), Receiver) as server:
        server.socket = context.wrap_socket(server.socket, server_side=True)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        host = f"https://localhost:{server.server_port}"
        code = r"""
import json, os, sys, time
from pathlib import Path
from ucode import launcher, metrics
metrics.begin('command')
runtime = metrics._runtime

def snapshot(event):
    directory = os.environ.get('UG_TEST_METRICS_DIAGNOSTICS')
    if not directory:
        return
    reporter = runtime.reporter
    data = {
        'event': event,
        'monotonic_ns': time.monotonic_ns(),
        'locked': runtime.lock.locked(),
        'reporter_started': reporter is not None,
        'reporter_alive': reporter is not None and reporter.poll() is None,
        'workspace_bound': runtime.workspace is not None,
        'credentials': len(runtime.credentials),
        'pending': len(runtime.pending),
        'unsent_bytes': len(runtime.unsent),
    }
    with (Path(directory) / 'parent.jsonl').open('a', encoding='utf-8') as output:
        output.write(json.dumps(data) + '\n')

snapshot('begin')
# Establish a contention-free precondition without waiting for the upload.
deadline = time.monotonic() + 3
assert runtime.test_idle is not None, 'test idle signal was not initialized'
directory = Path(os.environ['UG_TEST_METRICS_DIAGNOSTICS'])
while time.monotonic() < deadline:
    if any('"event": "reader-ready"' in p.read_text() for p in directory.glob('exporter-*.jsonl')):
        break
    time.sleep(0.01)
else:
    raise AssertionError('exporter pipe reader did not become ready')
assert runtime.test_idle.wait(max(0, deadline - time.monotonic())), 'exporter worker did not become idle'
assert runtime.reporter is not None, 'exporter was not started during CLI startup'
assert not runtime.lock.locked() and not runtime.wake.is_set(), 'exporter worker is not quiescent'
snapshot('startup-synchronized')
metrics.record('cli_import_duration_ms', 5)
snapshot('initial-record')
metrics.set_command('configure')
metrics.bind(sys.argv[1], 'test-workspace-token')
snapshot('bind')
if sys.argv[2] == 'launch':
    metrics.start_agent('claude')
    snapshot('agent-start')
    launcher.exec_or_spawn([sys.executable, '-c', "print('agent completed')"])
metrics.record('agent_configuration_duration_ms', 17, agent='claude')
snapshot('final-record')
metrics.finish()
snapshot('finish')
print('command completed')
"""
        diagnostics = tmp_path / "metrics-diagnostics"
        diagnostics.mkdir()
        env = os.environ.copy()
        env.update(
            UG_ENABLE_METRICS_EXPORT="1",
            UG_TEST_METRICS_DIAGNOSTICS=str(diagnostics),
            REQUESTS_CA_BUNDLE=str(certificate),
            OTEL_EXPORTER_OTLP_METRICS_CERTIFICATE=str(certificate),
            NO_PROXY="localhost,127.0.0.1",
        )
        env.pop("OTEL_SDK_DISABLED", None)

        def evidence():
            return "\n".join(
                f"{file.name}:\n{file.read_text(encoding='utf-8')}"
                for file in sorted(diagnostics.glob("*.jsonl"))
            )

        try:
            try:
                result = subprocess.run(
                    [sys.executable, "-c", code, host, "launch" if handoff else "exit"],
                    env=env,
                    cwd=tmp_path,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=4,
                )
            except subprocess.TimeoutExpired:
                pytest.fail(
                    f"Command exceeded 4 seconds; diagnostics:\n{evidence()}", pytrace=False
                )
            assert result.returncode == 0, f"{result.stderr}\n{evidence()}"
            assert result.stdout == ("agent completed\n" if handoff else "command completed\n")
            assert result.stderr == ""
            try:
                path, authorization, request = received.get(timeout=5)
            except queue.Empty:
                pytest.fail(f"Metrics were not received; diagnostics:\n{evidence()}", pytrace=False)
            assert path == "/api/2.0/ai-gateway/ug-cli/otel/v1/metrics"
            assert authorization == "Bearer test-workspace-token"
            logs = {
                file.name: file.read_text(encoding="utf-8") for file in diagnostics.glob("*.jsonl")
            }
            assert "parent.jsonl" in logs
            assert any('"event": "http-start"' in value for value in logs.values())
            assert all(
                "test-workspace-token" not in value and host not in value for value in logs.values()
            )
            points = {
                metric.name: metric.histogram.data_points[0]
                for resource in request.resource_metrics
                for scope in resource.scope_metrics
                for metric in scope.metrics
            }
            assert points["cli_import_duration_ms"].sum == 5
            final = "ug_startup_duration_ms" if handoff else "agent_configuration_duration_ms"
            assert points[final].count == 1
            if not handoff:
                assert points[final].sum == 17
            labels = {a.key: a.value.string_value for a in points[final].attributes}
            assert labels["command"] == "configure"
            assert labels["agent"] == "claude"
        finally:
            respond.set()
            server.shutdown()
            server_thread.join(timeout=2)


def test_idle_signal_is_allocated_only_for_diagnostics(tmp_path, monkeypatch):
    from ucode.metrics import Recorder

    monkeypatch.delenv("UG_TEST_METRICS_DIAGNOSTICS", raising=False)
    assert Recorder("command", 0).test_idle is None
    monkeypatch.setenv("UG_TEST_METRICS_DIAGNOSTICS", str(tmp_path))
    assert isinstance(Recorder("command", 0).test_idle, threading.Event)


def test_exporter_diagnostics_record_state_without_credentials(tmp_path, monkeypatch):
    import io

    from ucode.metrics.exporter import ExportBuffer

    monkeypatch.setenv("UG_TEST_METRICS_DIAGNOSTICS", str(tmp_path))
    buffer = ExportBuffer()
    buffer.receive(
        {
            "workspace": "https://private.example",
            "credentials": {"https://private.example": ["synthetic-secret", {}]},
            "observations": [[None, "cli_import_duration_ms", 3, {}]],
            "flush": True,
        }
    )
    buffer.read(io.BytesIO(b""))
    [path] = tmp_path.glob("exporter-*.jsonl")
    text = path.read_text()
    events = [json.loads(line) for line in text.splitlines()]
    assert {event["event"] for event in events} >= {"frame", "reader-ready", "eof"}
    frame = next(event for event in events if event["event"] == "frame")
    assert frame["credentials"] == 1 and frame["pending"] == 1
    assert all(isinstance(event["monotonic_ns"], int) for event in events)
    assert "synthetic-secret" not in text and "private.example" not in text


def test_exporter_buffer_bounds_unbound_observations_and_preserves_workspace_tokens():
    from ucode.metrics.exporter import ExportBuffer

    buffer = ExportBuffer()
    buffer.receive(
        {
            "context": {"command": "command"},
            "observations": [
                [None, "cli_import_duration_ms", n, {"command": "command"}] for n in range(1500)
            ],
        }
    )
    assert len(buffer.pending) == 1000
    assert buffer.drain() == []
    first, second = "https://first.example.invalid", "https://second.example.invalid"
    buffer.receive(
        {
            "workspace": first,
            "context": {"command": "configure", "agent": "claude"},
            "credentials": {first: ["first-token", {}], second: ["second-token", {}]},
        }
    )
    batches = buffer.drain()
    assert len(batches) == 1
    assert batches[0]["workspace"] == first
    assert batches[0]["token"] == "first-token"
    assert [row[1] for row in batches[0]["observations"]] == list(range(500, 1500))
    assert batches[0]["observations"][0][2] == {"command": "configure", "agent": "claude"}
    buffer.receive({"workspace": second, "observations": [[second, "ug_usage_duration_ms", 9, {}]]})
    assert buffer.drain()[0]["token"] == "second-token"


def test_incomplete_frame_is_not_interpreted_as_a_second_batch():
    import io

    from ucode.metrics.exporter import ExportBuffer

    buffer = ExportBuffer()
    message = json.dumps({"observations": [[None, "cli_import_duration_ms", 3, {}]]}).encode()
    buffer.read(io.BytesIO(message + b"\n" + message[:20]))
    assert len(buffer.pending) == 1
    assert buffer.closed.is_set()
