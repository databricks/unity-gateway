"""Disposable certificate authority for the TLS trust-store tests.

``python tests/tls_ca.py <directory>`` writes ``ca.pem`` and ``ca-key.pem`` there. CI runs it and
adds ``ca.pem`` to a runner's OS trust store before the tests, then names the directory in
``UG_TEST_OS_TRUSTED_CA_DIR``; nothing here touches a trust store itself. Never commit the output.
"""

from __future__ import annotations

import ipaddress
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CA_CERTIFICATE = "ca.pem"
CA_KEY = "ca-key.pem"


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def write_ca(directory: Path) -> None:
    """Write a new root CA's certificate and key to ``directory``."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = _name("Unity Gateway test root (disposable)")
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    directory.mkdir(parents=True, exist_ok=True)
    (directory / CA_CERTIFICATE).write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    (directory / CA_KEY).write_bytes(_pem(key))


def write_localhost_certificate(ca_directory: Path, path: Path) -> None:
    """Write a ``localhost`` server certificate and key, signed by the CA, to ``path``."""
    ca_certificate = x509.load_pem_x509_certificate((ca_directory / CA_CERTIFICATE).read_bytes())
    ca_key = serialization.load_pem_private_key((ca_directory / CA_KEY).read_bytes(), None)
    assert isinstance(ca_key, ec.EllipticCurvePrivateKey)
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(_name("localhost"))
        .issuer_name(ca_certificate.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(hours=12))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM) + _pem(key))


if __name__ == "__main__":
    write_ca(Path(sys.argv[1]))
