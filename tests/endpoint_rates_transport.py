"""Loopback CONNECT/TLS transport for endpoint-rate component tests."""

from __future__ import annotations

import datetime
import json
import os
import ssl
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

WORKSPACE = "https://rates.test"
OTHER_WORKSPACE = "https://other.test"
SYNTHETIC_BEARER = "local-endpoint-rates-fixture"


class EndpointRatesTransport:
    def __init__(self, root: Path, payload, *, status=200, hold=False, disconnect=False):
        self.requests = []
        self.targets = []
        self.requested = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.errors = []
        if not hold:
            self.release.set()

        root.mkdir(parents=True, exist_ok=True)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Endpoint rates fixture")])
        now = datetime.datetime.now(datetime.UTC)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(hours=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(
                x509.SubjectAlternativeName(
                    [x509.DNSName("rates.test"), x509.DNSName("other.test")]
                ),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        self.ca = root / "ca.pem"
        self.ca.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_path = root / "key.pem"
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        key_path.chmod(0o600)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.ca, key_path)
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                fixture.requests.append(
                    {"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}}
                )
                fixture.requested.set()
                if not fixture.release.wait(timeout=30):
                    raise TimeoutError("endpoint-rate response was not released")
                if not disconnect:
                    body = json.dumps(payload).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    self.wfile.flush()
                self.close_connection = True

            def do_CONNECT(self):
                fixture.targets.append(self.path)
                if self.path not in {"rates.test:443", "other.test:443"}:
                    self.send_error(502, "Unexpected destination")
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.flush()
                self.connection.settimeout(35)
                try:
                    with context.wrap_socket(self.connection, server_side=True) as connection:
                        Handler(connection, self.client_address, self.server)
                except Exception as exc:
                    fixture.errors.append(repr(exc))
                finally:
                    fixture.finished.set()
                    self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def child_environment(self, home: Path) -> dict[str, str]:
        home.mkdir(parents=True, exist_ok=True)
        return {
            **{key: os.environ[key] for key in ("SYSTEMROOT", "TEMP", "TMP") if key in os.environ},
            "PATH": os.pathsep.join([str(Path(sys.executable).parent), os.defpath]),
            "HOME": str(home),
            "USERPROFILE": str(home),
            "PYTHONNOUSERSITE": "1",
            "SSL_CERT_FILE": str(self.ca),
            "HTTPS_PROXY": f"http://127.0.0.1:{self.server.server_port}",
            "NO_PROXY": "127.0.0.1,localhost",
        }

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        assert not self.thread.is_alive()
        assert not self.errors, self.errors
