"""Loopback relay for GitHub Copilot signed in with the developer's own Copilot seat.

Copilot sends every Copilot API call to ``COPILOT_API_URL``, which ug sets to
``http://127.0.0.1:<port>/<session secret>``. Inference, the model catalog and Fusion's model
choice go to the workspace's AI Gateway: the caller's GitHub token stays in ``Authorization`` and a
Databricks token rides the swap header, with the Model Provider Service and the caller's Copilot
API host. Every other Copilot API call goes straight to that Copilot API host with the caller's
own headers. Copilot's OTLP export posts below the same secret: traces go to the gateway's OTel
route with a Databricks token in ``Authorization``, and metrics, which the gateway doesn't take,
get an empty success. Responses are relayed as they arrive, except that a gateway 403 becomes a 400
carrying the gateway's message, because Copilot hides 401 and 403 bodies but shows 400 bodies.

Security invariants:
  - Binds 127.0.0.1 and serves only requests addressed to it, from no browser origin, whose first
    path segment is the session secret. GitHub MCP, which Copilot sends without the base path, is
    the one exception and only ever goes direct.
  - A Databricks token is added only on the exact gateway routes and the OTel trace route. A
    direct call carries no Databricks header and goes only to a GitHub Copilot API host, without
    following redirects.
  - Never logs header values or bodies.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import httpx

from ucode.gateway_proxy import (
    AI_GATEWAY_TOKEN_HEADER,
    AUTHORIZATION_HEADER,
    HOP_BY_HOP_HEADERS,
    MODEL_PROVIDER_SERVICE_HEADER,
    TokenCache,
    _ProxyHandler,
    _start_proxy,
    log_proxy_diagnostic,
    log_token_refresh_failure,
)

GITHUB_COPILOT_API_HOST_HEADER = "x-databricks-github-copilot-api-host"

# Copilot API calls served by AI Gateway, by method and path below the session secret.
GATEWAY_ROUTES: Mapping[tuple[str, str], str] = {
    ("POST", "/chat/completions"): "/ai-gateway/openai/v1/chat/completions",
    ("POST", "/responses"): "/ai-gateway/openai/v1/responses",
    ("POST", "/v1/messages"): "/ai-gateway/anthropic/v1/messages",
    ("GET", "/meta"): "/ai-gateway/github-copilot/v1/meta",
    ("GET", "/models"): "/ai-gateway/v1/models",
    ("POST", "/auto"): "/ai-gateway/github-copilot/v1/auto",
}
# Where Copilot's OTLP exporter posts, below the session secret.
OTEL_PATH = "/ai-gateway/otel"
_OTEL_TRACES_CALL = ("POST", f"{OTEL_PATH}/v1/traces")
_OTEL_METRICS_CALL = ("POST", f"{OTEL_PATH}/v1/metrics")
# A session credential the exporter may send, which must not reach the gateway.
_OTEL_SESSION_CREDENTIAL_HEADER = "x-api-key"
# Headers the relay sets itself, which a launch's added headers can't replace.
_RESERVED_ADDED_HEADERS = frozenset(
    name.lower()
    for name in (
        AI_GATEWAY_TOKEN_HEADER,
        AUTHORIZATION_HEADER,
        MODEL_PROVIDER_SERVICE_HEADER,
        GITHUB_COPILOT_API_HOST_HEADER,
    )
)
# Copilot joins GitHub MCP to the bare host and port, dropping the session secret.
_SECRETLESS_DIRECT_CALLS = frozenset({("POST", "/mcp/readonly")})

_GITHUB_TOKEN_PREFIXES = ("gho_", "ghu_", "github_pat_", "tid=")
_COPILOT_API_HOST = re.compile(
    r"api(\.(individual|business|enterprise))?\.githubcopilot\.com"
    r"|copilot-api\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.ghe\.com"
)
_DATABRICKS_HEADER_PREFIXES = ("x-databricks-", "databricks-")

_GITHUB_COPILOT_USER_URL = "https://api.github.com/copilot_internal/user"
_GITHUB_LOOKUP_TIMEOUT = httpx.Timeout(10.0)
# A GitHub lookup that failed isn't retried for this long, so every request doesn't wait on it.
_GITHUB_MISS_TTL_S = 60.0


def copilot_api_host(url: object) -> str | None:
    """The host of ``url`` (a Copilot ``endpoints.api``), or None unless it is one of GitHub's
    Copilot API hosts over HTTPS on the default port."""
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if (
        parts.scheme != "https"
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        return None
    host = parts.hostname or ""
    return host if _COPILOT_API_HOST.fullmatch(host) else None


def _copilot_user_cache_file() -> Path:
    return Path.home() / ".cache" / "copilot" / "copilot-user-cache.json"


def _retrieved_at(entry: Mapping[str, object]) -> float:
    value = entry.get("retrievedAt")
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return float("-inf")


def cached_copilot_api_host(cache_file: Path | None = None) -> str | None:
    """The Copilot API host of the newest entry in Copilot's local user cache, or None."""
    path = cache_file or _copilot_user_cache_file()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    # The file is JSONC: whole-line `//` comments around a JSON object.
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("//")]
    try:
        payload = json.loads("\n".join(lines))
    except ValueError:
        return None
    best: tuple[float, str] | None = None
    for entry in payload.values() if isinstance(payload, dict) else ():
        if not isinstance(entry, dict):
            continue
        endpoints = entry.get("endpoints")
        host = copilot_api_host(endpoints.get("api")) if isinstance(endpoints, dict) else None
        if host and (best is None or _retrieved_at(entry) > best[0]):
            best = (_retrieved_at(entry), host)
    return best[1] if best else None


class CopilotHostResolver:
    """Finds the caller's Copilot API host, once per GitHub credential.

    Asks GitHub (``/copilot_internal/user``, which Copilot itself calls on every launch) with the
    caller's own credential. When that isn't possible (a GHE.com account, VS Code's short-lived
    token, GitHub unreachable), it falls back to the newest entry of Copilot's local user cache,
    which Copilot writes before its first Copilot API call."""

    def __init__(
        self, *, client: httpx.Client | None = None, cache_file: Path | None = None
    ) -> None:
        self._client = client
        self._cache_file = cache_file
        self._hosts: dict[str, str] = {}
        self._missed_at: dict[str, float] = {}
        self._lock = threading.Lock()

    def __call__(self, authorization: str) -> str | None:
        key = hashlib.sha256(authorization.encode()).hexdigest()
        with self._lock:
            known = self._hosts.get(key)
            missed_at = self._missed_at.get(key)
        if known:
            return known
        if missed_at is not None and time.monotonic() - missed_at < _GITHUB_MISS_TTL_S:
            return self._from_cache_file()
        host = self._from_github(authorization)
        if host is None:
            # The cache file can hold another account's newest entry, so it is never remembered
            # for this credential; a later GitHub answer can still replace it.
            with self._lock:
                self._missed_at[key] = time.monotonic()
            return self._from_cache_file()
        with self._lock:
            self._hosts[key] = host
            self._missed_at.pop(key, None)
        return host

    def _from_github(self, authorization: str) -> str | None:
        if not authorization.startswith(("Bearer gho_", "Bearer ghu_", "Bearer github_pat_")):
            return None
        headers = {AUTHORIZATION_HEADER: authorization, "Accept": "application/json"}
        try:
            if self._client is not None:
                response = self._client.get(_GITHUB_COPILOT_USER_URL, headers=headers)
            else:
                with httpx.Client(timeout=_GITHUB_LOOKUP_TIMEOUT, follow_redirects=False) as client:
                    response = client.get(_GITHUB_COPILOT_USER_URL, headers=headers)
            if response.status_code != 200:
                return None
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        endpoints = payload.get("endpoints") if isinstance(payload, dict) else None
        return copilot_api_host(endpoints.get("api")) if isinstance(endpoints, dict) else None

    def _from_cache_file(self) -> str | None:
        return cached_copilot_api_host(self._cache_file)


def _is_normalized(path: str) -> bool:
    # A fragment would miss the gateway routes and then be dropped on the way upstream. A percent escape
    # would miss them too, and an upstream that decodes it could serve a route the gateway never saw.
    if not path.startswith("/") or "\\" in path or "#" in path or "%" in path:
        return False
    return all(segment not in ("", ".", "..") for segment in path[1:].split("/"))


def _error_body(message: str, source: str | None = None) -> bytes:
    # Readable as both an OpenAI and an Anthropic error, whichever API Copilot called.
    error: dict[str, str] = {"type": "invalid_request_error", "message": message}
    if source:
        error["source"] = source
    return json.dumps({"type": "error", "error": error}).encode()


def _log_direct_request(method: str, path: str) -> None:
    log_proxy_diagnostic("copilot_direct_request", method=method, path=path)


class _CopilotRelayHandler(_ProxyHandler):
    # Set by the server factory.
    session_secret: str
    provider_service: str
    added_headers: Mapping[str, str]
    sign_in_hint: str
    copilot_host: Callable[[str], str | None]
    direct_base_url: Callable[[str], str]
    on_direct_request: Callable[[str, str], None]
    request_headers_adder: Callable[[Any, bytes | None], Mapping[str, str]] | None = None

    _api_host = ""
    _gateway_call = False

    def _handle(self) -> None:
        self._gateway_call = False
        raw_path, _, query = self.path.partition("?")
        if not _is_normalized(raw_path):
            self._reject(404, "This path is not served by the Unity Gateway Copilot relay.")
            return
        port = cast("tuple[str, int]", self.server.server_address)[1]
        if self.headers.get_all("Host") != [f"127.0.0.1:{port}"]:
            self._reject(403, "Requests must be addressed to the Unity Gateway Copilot relay.")
            return
        if self.headers.get("Origin") is not None:
            self._reject(403, "Browser requests are not served by the Unity Gateway Copilot relay.")
            return
        if self.headers.get("Transfer-Encoding") is not None:
            # Bodies are read by Content-Length only; the rest of a chunked body would be read
            # as the next request.
            self._send_json_error(
                411,
                "Unity Gateway's Copilot relay needs a Content-Length on request bodies.",
                close=True,
            )
            return
        call = self._relay_path(raw_path)
        if call is None:
            self._reject(404, "This path is not served by the Unity Gateway Copilot relay.")
            return
        if self.headers.get("Upgrade") is not None or (self.command, call) == ("GET", "/responses"):
            # Copilot retries a refused WebSocket as a plain POST /responses.
            self._reject(426, "WebSockets are not served by the Unity Gateway Copilot relay.")
            return
        suffix = f"?{query}" if query else ""
        if (self.command, call) == _OTEL_METRICS_CALL:
            self._discard()
            return
        if (self.command, call) == _OTEL_TRACES_CALL:
            self.path = call + suffix
            super()._handle()
            return
        authorization = self._github_authorization()
        if authorization is None:
            self._reject(401, "Sign in to GitHub Copilot to use the Unity Gateway Copilot relay.")
            return
        host = self.copilot_host(authorization)
        if host is None:
            self._reject(
                400,
                "Unity Gateway couldn't find your GitHub Copilot API host. Sign in to GitHub "
                "Copilot again, then restart it.",
            )
            return
        gateway_path = (
            None
            if (self.command, call) in _SECRETLESS_DIRECT_CALLS
            else GATEWAY_ROUTES.get((self.command, call))
        )
        if gateway_path is None:
            self._forward_direct(host, call + suffix)
            return
        self._api_host = host
        self._gateway_call = True
        self.path = gateway_path + suffix
        super()._handle()

    def _relay_path(self, path: str) -> str | None:
        """The Copilot API path below the session secret, or None when the secret is missing."""
        if (self.command, path) in _SECRETLESS_DIRECT_CALLS:
            return path
        secret, sep, rest = path[1:].partition("/")
        if not sep or not hmac.compare_digest(secret.encode(), self.session_secret.encode()):
            return None
        return f"/{rest}"

    def _github_authorization(self) -> str | None:
        values = self.headers.get_all(AUTHORIZATION_HEADER) or []
        if len(values) != 1:
            return None
        scheme, _, credential = values[0].partition(" ")
        if scheme.lower() != "bearer" or not credential.startswith(_GITHUB_TOKEN_PREFIXES):
            return None
        return f"Bearer {credential}"

    def _databricks_headers(self) -> frozenset[str]:
        return frozenset(
            name.lower()
            for name in self.headers
            if name.lower().startswith(_DATABRICKS_HEADER_PREFIXES)
        )

    def _is_otel_export(self) -> bool:
        return self.path.split("?", 1)[0] == _OTEL_TRACES_CALL[1]

    def _forward_target(self, body: bytes | None) -> tuple[str, frozenset[str], str]:
        if self._is_otel_export():
            strip = self._databricks_headers() | {_OTEL_SESSION_CREDENTIAL_HEADER}
            return AUTHORIZATION_HEADER, strip, "copilot-otel"
        return AI_GATEWAY_TOKEN_HEADER, self._databricks_headers(), "copilot-gateway"

    def _request_headers(self, body: bytes | None) -> Mapping[str, str]:
        if self.request_headers_adder is None:
            return {}
        try:
            return self.request_headers_adder(self.headers, body)
        except Exception as exc:
            # The headers only label the request, so a failure must not cost the request.
            log_proxy_diagnostic("request_headers_failed", error=type(exc).__name__)
            return {}

    def _added_headers(self, body: bytes | None) -> dict[str, str]:
        extra = {
            name: value
            for name, value in self.added_headers.items()
            if name.lower() not in _RESERVED_ADDED_HEADERS
        }
        if self._is_otel_export():
            return extra
        launched = {name.lower() for name in extra}
        per_request = {
            name: value
            for name, value in self._request_headers(body).items()
            if name.lower() not in _RESERVED_ADDED_HEADERS and name.lower() not in launched
        }
        return {
            **extra,
            **per_request,
            MODEL_PROVIDER_SERVICE_HEADER: self.provider_service,
            GITHUB_COPILOT_API_HOST_HEADER: self._api_host,
        }

    def _on_refresh_failure(self, exc: RuntimeError) -> bool:
        self.cache.report_refresh_failure(exc)
        # Copilot hides the body of a 401 or 403, so the hint needs a status it shows.
        self._send_json_error(400, f"Databricks sign-in expired. {self.sign_in_hint}")
        return True

    def _auth_rejection_is_final(self, resp: httpx.Response) -> bool:
        if not self._gateway_call or resp.status_code != 403:
            return False
        return _classify_denial(_denial_bytes(resp)).final

    def _relay_response(self, resp: httpx.Response, **kwargs: Any) -> None:
        if not self._gateway_call or resp.status_code != 403:
            return super()._relay_response(resp, **kwargs)
        denial = _classify_denial(_denial_bytes(resp))
        log_proxy_diagnostic(
            "copilot_gateway_denial", request_id=kwargs.get("diagnostic_id"), source=denial.source
        )
        # Relayed through the base relay as a 400, so the error and byte counts are recorded.
        body = _error_body(denial.message, denial.source)
        denial = httpx.Response(
            400,
            headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
            stream=httpx.ByteStream(body),
        )
        return super()._relay_response(denial, **kwargs)

    def _forward_direct(self, host: str, path: str) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else None
        stripped = HOP_BY_HOP_HEADERS | self._databricks_headers()
        headers = {
            name: value for name, value in self.headers.items() if name.lower() not in stripped
        }
        if not any(name.lower() == "accept-encoding" for name in headers):
            headers["Accept-Encoding"] = "identity"
        self.on_direct_request(self.command, path.split("?", 1)[0])
        # The base relay records metrics from these labels, and only the gateway path sets them.
        self._metric_labels = {"proxy_mode": self.metrics_proxy_mode, "http_method": self.command}
        try:
            with self.client.stream(
                self.command, self.direct_base_url(host) + path, headers=headers, content=body
            ) as resp:
                self._relay_response(resp)
        except (BrokenPipeError, ConnectionResetError):
            return
        except httpx.HTTPError as exc:
            log_proxy_diagnostic("copilot_direct_error", error_type=type(exc).__name__)
            self._send_json_error(502, "GitHub Copilot couldn't be reached.")

    def _drain(self) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length:
            self.rfile.read(length)

    def _discard(self) -> None:
        self._drain()
        try:
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except OSError:
            pass

    def _reject(self, status: int, message: str) -> None:
        self._drain()
        self._send_json_error(status, message)

    def _send_json_error(self, status: int, message: str, *, close: bool = False) -> None:
        body = _error_body(message)
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if close:
                # Also makes this handler close the connection after the response.
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass


_GATEWAY_DENIAL_BODY_LIMIT = 64 * 1024
_GENERIC_GATEWAY_DENIAL = "Unity Gateway denied this request. Check your access to the model."
_GITHUB_DENIAL_HINT = (
    "Check your Copilot CLI policy setting at https://github.com/settings/copilot."
)
_GITHUB_MESSAGE_LIMIT = 300
# Databricks error codes of a denial that a fresh token can't change.
_POLICY_ERROR_CODES = frozenset({"PERMISSION_DENIED", "INVALID_PARAMETER_VALUE"})


class _Denial:
    """A 403 from a gateway call, read into the message Copilot shows and where it came from:
    ``gateway`` (a Databricks error body), ``github`` (GitHub's own body, passed through by the
    gateway on the catalog routes) or ``unknown``."""

    __slots__ = ("final", "message", "source")

    def __init__(self, source: str, message: str, *, final: bool) -> None:
        self.source = source
        self.message = message
        self.final = final


def _denial_bytes(resp: httpx.Response) -> bytes | None:
    """The 403 body up to the limit, or None when it is missing, oversized or unreadable."""
    buffer = bytearray()
    try:
        for chunk in resp.iter_bytes():
            buffer.extend(chunk)
            if len(buffer) > _GATEWAY_DENIAL_BODY_LIMIT:
                return None
    except httpx.HTTPError:
        return None
    return bytes(buffer)


def _github_denial(message: str) -> str:
    text = " ".join(message.split())[:_GITHUB_MESSAGE_LIMIT].rstrip(" .")
    return f"GitHub Copilot denied this request: {text}. {_GITHUB_DENIAL_HINT}"


def _classify_denial(body: bytes | None) -> _Denial:
    if not body:
        return _Denial("unknown", _GENERIC_GATEWAY_DENIAL, final=False)
    try:
        payload = json.loads(body)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        message = payload.get("message")
        if not isinstance(message, str) or not message.strip():
            return _Denial("unknown", _GENERIC_GATEWAY_DENIAL, final=False)
        if "error_code" in payload:
            code = payload["error_code"]
            if not isinstance(code, str):
                return _Denial("unknown", _GENERIC_GATEWAY_DENIAL, final=False)
            return _Denial("gateway", message, final=code in _POLICY_ERROR_CODES)
        # GitHub's REST errors are `{"message": ...}` with no error code.
        return _Denial("github", _github_denial(message), final=True)
    try:
        text = body.decode("utf-8").strip()
    except UnicodeDecodeError:
        return _Denial("unknown", _GENERIC_GATEWAY_DENIAL, final=False)
    # The Copilot API answers a policy denial with one line of plain text.
    if text and not text.startswith("<") and "\n" not in text and text.isprintable():
        return _Denial("github", _github_denial(text), final=True)
    return _Denial("unknown", _GENERIC_GATEWAY_DENIAL, final=False)


def _https_base_url(host: str) -> str:
    return f"https://{host}"


def start_copilot_relay_proxy(
    workspace: str,
    token_provider: Callable[[bool], str],
    *,
    session_secret: str,
    provider_service: str,
    sign_in_hint: str,
    added_headers: Mapping[str, str] | None = None,
    request_headers_adder: Callable[[Any, bytes | None], Mapping[str, str]] | None = None,
    copilot_host: Callable[[str], str | None] | None = None,
    direct_base_url: Callable[[str], str] = _https_base_url,
    on_direct_request: Callable[[str, str], None] = _log_direct_request,
    on_refresh_failure: Callable[[BaseException], None] = log_token_refresh_failure,
) -> tuple[ThreadingHTTPServer, TokenCache, httpx.Client]:
    """Start the seat relay for one Copilot session; Copilot's ``COPILOT_API_URL`` is
    ``http://127.0.0.1:<server.server_address[1]>/<session_secret>``, and its OTLP endpoint is that
    URL followed by :data:`OTEL_PATH`.

    ``added_headers`` go on every gateway call and OTel trace export, never on a direct one; they
    can't replace the credential, Model Provider Service or Copilot API host headers.
    :data:`GATEWAY_ROUTES` are the calls AI Gateway serves; every other call goes direct to the
    Copilot API host that ``copilot_host(authorization)`` finds for the caller (a
    :class:`CopilotHostResolver` by default), at ``direct_base_url(host)``.
    ``on_direct_request(method, path)`` sees each direct call's method and path, never its headers
    or body."""
    return _start_proxy(
        workspace,
        token_provider,
        0,
        upstream_path="",
        token_header=AI_GATEWAY_TOKEN_HEADER,
        handler_type=_CopilotRelayHandler,
        force_refresh_near_expiry=True,
        fallback_on_bind_error=False,
        handler_attrs={
            "session_secret": session_secret,
            "provider_service": provider_service,
            "added_headers": dict(added_headers or {}),
            "request_headers_adder": (
                staticmethod(request_headers_adder) if request_headers_adder else None
            ),
            "sign_in_hint": sign_in_hint,
            # staticmethod: a plain function on the handler class would bind as a method.
            "copilot_host": staticmethod(copilot_host or CopilotHostResolver()),
            "direct_base_url": staticmethod(direct_base_url),
            "on_direct_request": staticmethod(on_direct_request),
        },
        on_refresh_failure=on_refresh_failure,
    )
