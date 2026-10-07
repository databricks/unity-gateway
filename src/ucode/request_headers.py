"""Validated, invocation-scoped custom headers for workspace requests."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urljoin, urlsplit

_CUSTOM_HEADERS: ContextVar[dict[str, str] | None] = ContextVar("custom_headers", default=None)
CUSTOM_HEADERS_ENV = "UCODE_CUSTOM_HEADERS"


_HTTP_HEADER_NAME_PATTERN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_PROTECTED_CUSTOM_HEADER_NAMES = frozenset(
    {
        "accept",
        "authorization",
        "connection",
        "content-length",
        "content-type",
        "cookie",
        "databricks-model-provider-service",
        "databricks-model-service-parent-schema",
        "databricks-smart-router-recipe",
        "host",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "user-agent",
        "x-api-key",
        "x-databricks-ai-gateway-token",
        "x-databricks-use-coding-agent-mode",
    }
)


def parse_custom_headers(values: list[str] | None) -> dict[str, str]:
    """Parse repeatable ``--header 'Name: value'`` options."""
    parsed: dict[str, tuple[str, str]] = {}
    for item in values or []:
        name, separator, value = item.partition(":")
        name = name.strip()
        if not separator or _HTTP_HEADER_NAME_PATTERN.fullmatch(name) is None:
            raise RuntimeError("--header must use the format `Name: value` with a valid name.")
        value = value.strip()
        if any(
            ord(character) < 32 or ord(character) == 127 or character in "\u0085\u2028\u2029"
            for character in value
        ):
            raise RuntimeError(
                "--header values cannot contain control characters or line separators."
            )
        normalized_name = name.casefold()
        if normalized_name in _PROTECTED_CUSTOM_HEADER_NAMES:
            raise RuntimeError(f"--header cannot override protected header '{name}'.")
        parsed[normalized_name] = (name, value)
    return dict(parsed.values())


def get_custom_headers() -> dict[str, str]:
    return dict(_CUSTOM_HEADERS.get() or {})


@contextmanager
def custom_header_scope(headers: dict[str, str]) -> Iterator[None]:
    token = _CUSTOM_HEADERS.set(dict(headers))
    try:
        yield
    finally:
        _CUSTOM_HEADERS.reset(token)


@contextmanager
def custom_header_environment(workspace: str, headers: dict[str, str]) -> Iterator[None]:
    """Pass the launch's headers only to helpers that explicitly opt in."""
    previous = os.environ.get(CUSTOM_HEADERS_ENV)
    if headers:
        os.environ[CUSTOM_HEADERS_ENV] = json.dumps({"workspace": workspace, "headers": headers})
    else:
        os.environ.pop(CUSTOM_HEADERS_ENV, None)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(CUSTOM_HEADERS_ENV, None)
        else:
            os.environ[CUSTOM_HEADERS_ENV] = previous


def inherited_custom_headers(workspace: str) -> dict[str, str]:
    """Read launch headers in a routing helper for the same workspace only."""
    raw = os.environ.get(CUSTOM_HEADERS_ENV)
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict) or not isinstance(payload.get("workspace"), str):
            raise ValueError
        if _origin(workspace) != _origin(payload["workspace"]):
            return {}
        headers = payload["headers"]
        if not isinstance(headers, dict) or not all(
            isinstance(name, str) and isinstance(value, str) for name, value in headers.items()
        ):
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise RuntimeError("Invalid custom-header context inherited from ug.") from None
    return parse_custom_headers([f"{name}: {value}" for name, value in headers.items()])


def _origin(url: str) -> tuple[str, str | None, int | None]:
    parsed = urlsplit(url)
    port = parsed.port
    if port is None:
        port = {"http": 80, "https": 443}.get(parsed.scheme)
    return parsed.scheme, parsed.hostname, port


class _ScopedHeaderRedirectHandler(urllib_request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urljoin(req.full_url, newurl)
        if _origin(req.full_url) != _origin(target):
            raise urllib_error.HTTPError(
                req.full_url, code, "Cross-origin redirect blocked for custom headers", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, target)


def urlopen(request: urllib_request.Request, *, timeout: float, scoped_headers: bool = False):
    """Preserve custom headers on same-origin redirects; reject cross-origin redirects."""
    if scoped_headers:
        return urllib_request.build_opener(_ScopedHeaderRedirectHandler()).open(
            request, timeout=timeout
        )
    return urllib_request.urlopen(request, timeout=timeout)
