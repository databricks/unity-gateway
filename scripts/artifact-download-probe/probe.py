import hashlib
import ipaddress
import json
import os
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

EXPECTED_SHA256 = "fea144e6a01bc38aa109ae971cd28f5f1c8c470de5e41170a0d1121dab01eeee"
ARTIFACT_HOST = "databricks-build-artifacts-manual-staging.s3.us-west-2.amazonaws.com"
SERVICE_HOSTS = (
    "bazel-cache.staging.cloud.databricks.com",
    "bazel-cache.private.staging.cloud.databricks.com",
)


def service_reachability(host):
    result = {"host": host, "client_certificate_supplied": False}
    try:
        addresses = socket.getaddrinfo(host, 444, type=socket.SOCK_STREAM)
        result["dns_resolved"] = bool(addresses)
        result["private_addresses"] = any(
            ipaddress.ip_address(address[4][0]).is_private for address in addresses
        )
        with socket.create_connection((host, 444), timeout=5) as connection:
            result["tcp_connected"] = True
            context = ssl.create_default_context()
            context.set_alpn_protocols(["h2"])
            with context.wrap_socket(connection, server_hostname=host) as tls:
                result["tls_verified"] = True
                result["alpn"] = tls.selected_alpn_protocol()
    except Exception as error:
        result["error_type"] = type(error).__name__
    return result


def download(signed_url):
    result = {}
    parsed = urllib.parse.urlsplit(signed_url)
    if parsed.scheme != "https" or parsed.hostname != ARTIFACT_HOST:
        return {"passed": False, "error": "Missing or unexpected signed artifact URL"}
    unsigned_url = urllib.parse.urlunsplit(parsed._replace(query="", fragment=""))
    try:
        with urllib.request.urlopen(unsigned_url, timeout=20) as response:
            result["unsigned_status"] = response.status
    except urllib.error.HTTPError as error:
        result["unsigned_status"] = error.code
    except Exception as error:
        result["unsigned_error_type"] = type(error).__name__
    try:
        digest = hashlib.sha256()
        size = 0
        with urllib.request.urlopen(signed_url, timeout=45) as response:
            result["signed_status"] = response.status
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
        result["bytes"] = size
        result["sha256"] = digest.hexdigest()
        result["passed"] = digest.hexdigest() == EXPECTED_SHA256
    except urllib.error.HTTPError as error:
        result.update(signed_status=error.code, passed=False)
    except Exception as error:
        result.update(error_type=type(error).__name__, passed=False)
    return result


def main():
    result = {
        "platform": os.environ.get("RUNNER_OS"),
        "artifact": "tzdb.tar",
        "download": download(os.environ.get("ARTIFACT_DOWNLOAD_URL", "")),
        "services": [service_reachability(host) for host in SERVICE_HOSTS],
    }
    output = json.dumps(result, indent=2)
    Path("artifact-probe-result.json").write_text(output + "\n")
    print(output)
    return 0 if result["download"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
