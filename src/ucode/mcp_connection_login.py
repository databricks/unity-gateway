"""Browser sign-in for connection-backed MCP services, driven by the proxy on demand.

The browser saves the user's SaaS credential; the selected CLI profile's workspace
session stays unchanged. Poll that user's credential before retrying the MCP request.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from urllib.parse import unquote, urlencode

from ucode.databricks import (
    AIGW_MCP_SERVICES_SEGMENT,
    get_databricks_token,
    get_mcp_service,
    get_mcp_service_user_credential,
    workspace_org_id,
    workspace_origin,
)
from ucode.os_compatibility import subprocess_cross_os

_LOGIN_TIMEOUT_SECONDS = 300
_POLL_INTERVAL_SECONDS = 2


def _retryable_credential_error(reason: str) -> bool:
    return re.match(r"^(?:HTTP (?:429|5[0-9]{2}) |network error:)", reason) is not None


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
    try:
        token = get_databricks_token(workspace, profile)
    except RuntimeError as exc:
        return False, str(exc)
    service, reason = get_mcp_service(workspace, token, connection)
    if reason or not isinstance(service, dict):
        return (
            False,
            f"could not find MCP service for connection sign-in: {reason or 'invalid metadata'}",
        )
    org_id = workspace_org_id(workspace)
    if not org_id:
        return (
            False,
            "could not determine workspace ID for connection sign-in (workspace response omitted its ID)",
        )
    for attempt in range(3):
        initial_credential, reason = get_mcp_service_user_credential(workspace, token, connection)
        if not reason or not _retryable_credential_error(reason) or attempt == 2:
            break
        time.sleep(_POLL_INTERVAL_SECONDS)
    if reason and not reason.startswith("HTTP 404 "):
        return False, f"could not check connection sign-in: {reason}"
    initial_active = (
        isinstance(initial_credential, dict)
        and isinstance(initial_credential.get("provisioning_info"), dict)
        and initial_credential["provisioning_info"].get("state") == "ACTIVE"
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
        credential, reason = get_mcp_service_user_credential(
            workspace, token, connection, timeout=min(10, max(0.001, deadline - time.monotonic()))
        )
        if time.monotonic() >= deadline:
            break
        state = None
        if reason:
            if not reason.startswith("HTTP 404 ") and not _retryable_credential_error(reason):
                return False, f"could not check connection sign-in: {reason}"
        else:
            provisioning = (
                credential.get("provisioning_info") if isinstance(credential, dict) else None
            )
            state = provisioning.get("state") if isinstance(provisioning, dict) else None
            if state == "ACTIVE":
                if not initial_active or credential != initial_credential:
                    return True, "signed in"
            elif state not in {"FAILED", "PROVISIONING", "UPDATING", "DELETING"}:
                return (
                    False,
                    "could not check connection sign-in: missing or unknown credential state",
                )
        if (reason and reason.startswith("HTTP 404 ")) or (not reason and state != "ACTIVE"):
            initial_active = False
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
