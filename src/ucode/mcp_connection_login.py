"""Browser sign-in for connection-backed MCP services, driven by the proxy on demand.

The browser saves the user's SaaS credential; the selected CLI profile's workspace
session stays unchanged. Poll that user's credential before retrying the MCP request.
"""

from __future__ import annotations

import subprocess
import sys
import time
from urllib.parse import quote, unquote, urlencode

from ucode.databricks import (
    AIGW_MCP_SERVICES_SEGMENT,
    _http_get_json,
    get_databricks_token,
    workspace_org_id,
    workspace_origin,
)
from ucode.os_compatibility import subprocess_cross_os

_LOGIN_TIMEOUT_SECONDS = 300
_POLL_INTERVAL_SECONDS = 2


def connection_from_url(url: str) -> str | None:
    """Return the connection FQN of an AI Gateway MCP service URL, or ``None``.

    ``https://ws/ai-gateway/mcp-services/system.ai.github`` -> ``system.ai.github``.
    A URL that is not an mcp-services endpoint (or names no service) returns
    ``None`` — only connection-backed services get automatic connection login.
    """
    marker = url.find(AIGW_MCP_SERVICES_SEGMENT)
    if marker == -1:
        return None
    tail = url[marker + len(AIGW_MCP_SERVICES_SEGMENT) :]
    # Strip any trailing path (``/tools/list``), query, or fragment.
    connection = tail.split("/")[0].split("?")[0].split("#")[0]
    return connection or None


def run_connection_login(
    resource_url: str,
    workspace: str,
    *,
    profile: str | None = None,
) -> tuple[bool, str]:
    """Open MCP service sign-in and wait for the selected profile's credential.

    All diagnostics go to stderr because stdout carries MCP JSON-RPC messages.
    Browser launch alone never establishes successful authentication.
    """
    connection = unquote(connection_from_url(resource_url) or "")
    if len(connection.split(".")) != 3 or not all(connection.split(".")):
        return False, "connection sign-in requires an MCP service name (catalog.schema.service)"
    origin = workspace_origin(workspace)
    service_url = f"{origin}/api/2.1/unity-catalog/mcp-services/{quote(connection, safe='')}"
    try:
        token = get_databricks_token(workspace, profile)
    except RuntimeError as exc:
        return False, str(exc)
    org_id = workspace_org_id(workspace)
    if not org_id:
        _, reason = _http_get_json(service_url, token)
        org_id = workspace_org_id(workspace)
        if not org_id:
            return False, (
                "could not determine workspace ID for connection sign-in"
                + (f": {reason}" if reason else " (workspace response omitted its ID)")
            )
    login_url = f"{origin}/mcp-service-login?{urlencode({'name': connection, 'o': org_id})}"
    print(
        f"Signing in to '{connection}'. Complete provider sign-in using the same Databricks "
        f"account as CLI profile '{profile or '(default)'}'. If the browser or provider popup "
        f"doesn't open, visit this URL and click Login:\n{login_url}",
        file=sys.stderr,
        flush=True,
    )
    deadline = time.monotonic() + _LOGIN_TIMEOUT_SECONDS
    try:
        # Isolate webbrowser's subprocess output from the MCP stdout wire, including text browsers.
        result = subprocess_cross_os.run(
            [
                sys.executable,
                "-c",
                "import sys, webbrowser; sys.exit(0 if webbrowser.open(sys.argv[1]) else 1)",
                login_url,
            ],
            check=False,
            timeout=10,
            stdin=subprocess.DEVNULL,
            stdout=sys.stderr,
            stderr=sys.stderr,
        )
        browser_opened = result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        browser_opened = False
    if not browser_opened:
        print("Could not open a browser. Open the sign-in URL above manually.", file=sys.stderr)

    while time.monotonic() < deadline:
        try:
            token = get_databricks_token(workspace, profile)
        except RuntimeError as exc:
            return False, str(exc)
        credential, reason = _http_get_json(f"{service_url}/user-credentials", token)
        if reason:
            if not reason.startswith(
                (
                    "HTTP 404 ",
                    "HTTP 429 ",
                    "HTTP 500 ",
                    "HTTP 502 ",
                    "HTTP 503 ",
                    "HTTP 504 ",
                    "network error:",
                )
            ):
                return False, f"could not check connection sign-in: {reason}"
        else:
            provisioning = (
                credential.get("provisioning_info") if isinstance(credential, dict) else None
            )
            state = provisioning.get("state") if isinstance(provisioning, dict) else None
            if state == "ACTIVE":
                return True, "signed in"
            if state != "FAILED":
                return (
                    False,
                    "could not check connection sign-in: missing or unknown credential state",
                )
        time.sleep(min(_POLL_INTERVAL_SECONDS, max(0, deadline - time.monotonic())))
    return False, (
        f"connection sign-in timed out after {_LOGIN_TIMEOUT_SECONDS} seconds. "
        f"Finish sign-in at {login_url} using the same Databricks account as CLI profile "
        f"'{profile or '(default)'}', then retry authentication in your agent."
    )


__all__ = [
    "connection_from_url",
    "run_connection_login",
]
