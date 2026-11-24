"""Real HTTPS: each client ``ug`` uses, against a local server whose CA only one store trusts.

The server's certificate is signed by a disposable CA. A fresh interpreter runs ``tls.apply()``
the way every ``ug`` process does, then makes one request per client. No ``ssl`` patching.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests import tls_ca

# A directory holding a CA (see tests/tls_ca.py) that this machine's OS trust store already
# trusts. CI sets it on the runners where that store is not a file OpenSSL reads.
OS_TRUSTED_CA_DIR_ENV_VAR = "UG_TEST_OS_TRUSTED_CA_DIR"
CLIENTS = ("urllib", "httpx", "requests", "databricks_sdk", "ug")

_CLIENT_SCRIPT = r"""
import json, ssl, sys
from ucode import tls
tls.apply()
url = sys.argv[1]
results = {}

def attempt(name, call):
    try:
        call()
        results[name] = "ok"
    except Exception as exc:
        results[name] = "certificate" if certificate_failure(exc) else f"{type(exc).__name__}: {exc}"

def certificate_failure(exc):
    # Clients wrap the verification error differently; some keep it only as text.
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        text = f"{type(exc).__name__}: {exc!r}"
        if isinstance(exc, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in text:
            return True
        if "SSLCertVerificationError" in text:
            return True
        exc = exc.__cause__ or exc.__context__
    return False

def with_urllib():
    import urllib.request
    urllib.request.urlopen(url, timeout=10).read()

def with_httpx():
    import httpx
    httpx.get(url, timeout=10).raise_for_status()

def with_requests():
    import requests
    requests.get(url, timeout=10).raise_for_status()

def with_databricks_sdk():
    from databricks.sdk.core import ApiClient, Config
    client = ApiClient(Config(host=url, token="test-token", retry_timeout_seconds=1))
    client.do("GET", "/api/2.0/preview/scim/v2/Me")

def with_ug():
    # ug's own requests: the pooled httpx client under the `os` store (with no proxy), and
    # urllib itself under `python` — each with its default certificate chain.
    from ucode.databricks import _http_get_json
    payload, reason = _http_get_json(url + "/api/anything", "test-token", max_retries=0)
    results["ug_reason"] = reason
    if reason and "certificate" in reason.lower():
        raise ssl.SSLCertVerificationError(reason)
    if reason:
        raise RuntimeError(reason)

attempt("urllib", with_urllib)
attempt("httpx", with_httpx)
attempt("requests", with_requests)
attempt("databricks_sdk", with_databricks_sdk)
attempt("ug", with_ug)
print(json.dumps(results))
"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def ca_directory(tmp_path) -> Path:
    trusted = os.environ.get(OS_TRUSTED_CA_DIR_ENV_VAR)
    if trusted:
        return Path(trusted)
    directory = tmp_path / "ca"
    tls_ca.write_ca(directory)
    return directory


class _IPv6Server(ThreadingHTTPServer):
    address_family = socket.AF_INET6


def _serve(certificate: Path):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate)
    servers = [ThreadingHTTPServer(("127.0.0.1", 0), _Handler)]
    port = servers[0].server_port
    # Clients resolve `localhost` to ::1 first where IPv6 exists. Without a listener there,
    # Windows spends about 2 s per client on the refused connection before trying 127.0.0.1.
    try:
        servers.append(_IPv6Server(("::1", port), _Handler))
    except OSError:
        pass  # No IPv6 loopback, so `localhost` resolves to 127.0.0.1 only.
    threads = []
    for server in servers:
        server.socket = context.wrap_socket(server.socket, server_side=True)
        threads.append(threading.Thread(target=server.serve_forever, daemon=True))
        threads[-1].start()
    try:
        yield f"https://localhost:{port}"
    finally:
        for server, thread in zip(servers, threads, strict=True):
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


@pytest.fixture
def https_server(tmp_path, ca_directory):
    certificate = tmp_path / "localhost.pem"
    tls_ca.write_localhost_certificate(ca_directory, certificate)
    yield from _serve(certificate)


@pytest.fixture
def untrusted_https_server(tmp_path):
    """A server whose CA was made for this test, so no store on any machine trusts it."""
    directory = tmp_path / "untrusted-ca"
    tls_ca.write_ca(directory)
    certificate = tmp_path / "untrusted-localhost.pem"
    tls_ca.write_localhost_certificate(directory, certificate)
    yield from _serve(certificate)


def _run_clients(url: str, tmp_path: Path, **environment: str) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "REQUESTS_CA_BUNDLE",
            "CURL_CA_BUNDLE",
            "UG_TLS_TRUST_STORE",
        }
        and not key.upper().endswith("_PROXY")
        and not key.startswith("DATABRICKS_")
    }
    # An empty home: no saved `tls_trust_store`, Databricks profile, or .netrc.
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env.update(HOME=str(home), USERPROFILE=str(home), NO_PROXY="localhost,127.0.0.1")
    env.update(environment)
    result = subprocess.run(
        [sys.executable, "-c", _CLIENT_SCRIPT, url],
        env=env,
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _outcomes(results: dict[str, str]) -> dict[str, str]:
    return {client: results[client] for client in CLIENTS}


def test_python_chain_with_ssl_cert_file_leaves_requests_clients_failing(
    https_server, ca_directory, tmp_path
):
    """Why the docs say to set both: `requests` (the SDK, metrics upload) ignores SSL_CERT_FILE."""
    results = _run_clients(
        https_server,
        tmp_path,
        UG_TLS_TRUST_STORE="python",
        SSL_CERT_FILE=str(ca_directory / tls_ca.CA_CERTIFICATE),
    )
    assert _outcomes(results) == {
        "urllib": "ok",
        "httpx": "ok",
        "requests": "certificate",
        "databricks_sdk": "certificate",
        "ug": "ok",
    }


def test_python_chain_with_both_bundle_variables_verifies_every_client(
    https_server, ca_directory, tmp_path
):
    bundle = str(ca_directory / tls_ca.CA_CERTIFICATE)
    results = _run_clients(
        https_server,
        tmp_path,
        UG_TLS_TRUST_STORE="python",
        SSL_CERT_FILE=bundle,
        REQUESTS_CA_BUNDLE=bundle,
    )
    assert _outcomes(results) == dict.fromkeys(CLIENTS, "ok")


@pytest.mark.parametrize("trust_store", ["os", "python"])
def test_untrusted_ca_fails_every_client_with_an_actionable_reason(
    untrusted_https_server, tmp_path, trust_store
):
    results = _run_clients(untrusted_https_server, tmp_path, UG_TLS_TRUST_STORE=trust_store)
    assert _outcomes(results) == dict.fromkeys(CLIENTS, "certificate")
    reason = results["ug_reason"]
    assert reason.startswith("network error: certificate verification failed: ")
    assert "('" not in reason
    other = "python" if trust_store == "os" else "os"
    assert f"UG_TLS_TRUST_STORE={other}" in reason
    assert "SSL_CERT_FILE and REQUESTS_CA_BUNDLE" in reason


def test_os_store_verifies_every_client_by_default(https_server, ca_directory, tmp_path):
    """The inspection-proxy case: the root is in the OS store only, and nothing else is set."""
    environment = {}
    if sys.platform.startswith("linux"):
        # Linux's OS store is the CA bundle OpenSSL is pointed at.
        environment["SSL_CERT_FILE"] = str(ca_directory / tls_ca.CA_CERTIFICATE)
    elif not os.environ.get(OS_TRUSTED_CA_DIR_ENV_VAR):
        pytest.skip(
            f"needs a test CA in this machine's trust store; set {OS_TRUSTED_CA_DIR_ENV_VAR} "
            "(see tests/tls_ca.py)"
        )
    results = _run_clients(https_server, tmp_path, **environment)
    assert _outcomes(results) == dict.fromkeys(CLIENTS, "ok")
