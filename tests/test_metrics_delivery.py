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
from cryptography.x509.oid import NameOID
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


@pytest.mark.parametrize("handoff", [False, True], ids=["command-exit", "agent-exec"])
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
import os, sys, time
from ucode import metrics
metrics.begin('command')
runtime = metrics._runtime
# Wait for OS process creation only in the test, then exercise an immediate exit/exec.
# The old implementation never starts an uploader here, so this is also a regression gate.
deadline = time.monotonic() + 3
while runtime.reporter is None and time.monotonic() < deadline:
    time.sleep(0.01)
assert runtime.reporter is not None, 'exporter was not started during CLI startup'
with runtime.lock:
    pass
metrics.record('cli_import_duration_ms', 5)
metrics.set_command('configure')
metrics.bind(sys.argv[1], 'test-workspace-token')
if sys.argv[2] == 'exec':
    metrics.start_agent('claude')
    metrics.agent_handoff()
    os.execv(sys.executable, [sys.executable, '-c', "print('agent completed')"])
metrics.record('agent_configuration_duration_ms', 17, agent='claude')
metrics.finish()
print('command completed')
"""
        env = os.environ.copy()
        env.update(
            UG_ENABLE_METRICS_EXPORT="1",
            REQUESTS_CA_BUNDLE=str(certificate),
            OTEL_EXPORTER_OTLP_METRICS_CERTIFICATE=str(certificate),
            NO_PROXY="localhost,127.0.0.1",
        )
        env.pop("OTEL_SDK_DISABLED", None)
        try:
            result = subprocess.run(
                [sys.executable, "-c", code, host, "exec" if handoff else "exit"],
                env=env,
                cwd=tmp_path,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=4,
            )
            assert result.returncode == 0, result.stderr
            assert result.stdout == ("agent completed\n" if handoff else "command completed\n")
            assert result.stderr == ""
            path, authorization, request = received.get(timeout=5)
            assert path == "/api/2.0/ai-gateway/ug-cli/otel/v1/metrics"
            assert authorization == "Bearer test-workspace-token"
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
