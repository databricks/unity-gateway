"""Temporary loopback TLS trusted only by the isolated test processes."""

from __future__ import annotations

import ipaddress
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path

import certifi
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def localhost_tls(directory: Path) -> tuple[ssl.SSLContext, Path]:
    directory.mkdir(parents=True, exist_ok=True)
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
    cert_path = directory / "localhost.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path = directory / "localhost-key.pem"
    key_path.touch(mode=0o600)
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    return context, cert_path


def trust_localhost_certificate(env: dict[str, str], certificate: Path) -> None:
    # Preserve public and explicitly configured roots for upstream/agent connections.
    roots = [
        ssl.DER_cert_to_PEM_cert(cert).encode()
        for cert in ssl.create_default_context().get_ca_certs(binary_form=True)
    ]
    paths = {certifi.where(), str(certificate)}
    for name in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS"):
        if path := env.get(name):
            paths.add(path)
    roots.extend(Path(path).read_bytes() for path in sorted(paths))
    bundle = certificate.with_name("trusted-roots.pem")
    bundle.write_bytes(b"\n".join(roots))
    for name in (
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
        "OTEL_EXPORTER_OTLP_METRICS_CERTIFICATE",
    ):
        env[name] = str(bundle)
    env["NO_PROXY"] = ",".join(filter(None, [env.get("NO_PROXY"), "localhost", "127.0.0.1"]))
