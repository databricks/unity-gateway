"""Read-only workspace JSON transport with strict origins and no redirects."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def workspace_origin(workspace: str, token: str) -> str:
    """Validate an explicit HTTPS origin and bearer before constructing a client."""
    assert isinstance(workspace, str), "Expected an HTTPS workspace origin"
    try:
        origin = urllib.parse.urlsplit(workspace)
        port = origin.port
    except ValueError:
        raise AssertionError("Expected a valid HTTPS workspace origin") from None
    assert (
        origin.scheme == "https"
        and origin.hostname
        and origin.username is None
        and origin.password is None
        and origin.path in {"", "/"}
        and not origin.query
        and not origin.fragment
        and (port is None or 0 < port <= 65535)
        and not re.search(r"[\s\\]", workspace)
    ), "Expected an HTTPS workspace origin without credentials"
    assert isinstance(token, str) and token.strip() and not re.search(r"[\r\n]", token), (
        "Expected an explicit workspace bearer"
    )
    return f"https://{origin.netloc}"


def safe_https_json_get(
    workspace: str, token: str, path: str, *, headers: dict[str, str] | None = None
) -> object:
    """GET JSON from an explicit workspace origin without redirects or secret diagnostics."""
    origin = workspace_origin(workspace, token)
    assert path.startswith("/") and not path.startswith("//"), "Expected a workspace API path"
    request = urllib.request.Request(
        f"{origin}{path}",
        headers={
            **(headers or {}),
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise AssertionError(f"Workspace JSON GET returned HTTP {error.code}") from None
    except (urllib.error.URLError, OSError, ValueError):
        raise AssertionError("Workspace JSON GET failed or returned invalid JSON") from None
