"""Per-connection login for AI Gateway MCP services, driven by the proxy on a 401.

A connection-backed AI Gateway MCP service (e.g. ``system.ai.github``) needs a
per-user connection credential before its tools can be used. Until the user has
logged in to the underlying SaaS, AI Gateway answers requests with an HTTP 401
(RFC 9728 ``WWW-Authenticate``).

The ``ug mcp-proxy`` bridge (see ``mcp_proxy``) sees that 401 in its httpx auth
flow and, for a connection-backed service, runs the Databricks CLI U2M login
with an RFC 8707 ``resource`` indicator naming the service, then retries the
request. A resource-aware ``/oidc`` drives the connection's own SaaS login
before minting the token, so the credential exists on retry — transparently to
the coding agent, which just sees the connection authenticate and succeed. This
is the behaviour of a generic OAuth MCP bridge (e.g. ``mcp-remote``), done in
ucode with the Databricks CLI so no extra library or per-agent OAuth app is
needed. Requires the CLI ``--resource`` flag (databricks/cli#6621).
"""

from __future__ import annotations

import subprocess
import sys
from urllib.parse import quote

from ucode.databricks import (
    _http_get_json,
    _scim_me,
    get_databricks_token,
    workspace_hostname,
)

# AI Gateway MCP service endpoints look like
# ``https://<ws>/ai-gateway/mcp-services/<catalog>.<schema>.<service>``.
AIGW_MCP_SERVICES_SEGMENT = "/ai-gateway/mcp-services/"

# Login can pop a browser and wait for the user to complete the SaaS login, so
# allow generously more than a token refresh would take.
_LOGIN_TIMEOUT_SECONDS = 300

# Connection securable kinds that use a per-user OAuth (U2M) credential — i.e. the
# service needs a connection sign-in. Anything else (PAT/basic/service-managed)
# has no per-user login to drive. Mirrors the webapp's known-OAuth-kinds set.
_OAUTH_U2M_CONNECTION_KINDS = frozenset(
    {
        "CONNECTION_HTTP_OAUTH_U2M_MAPPING",
        "CONNECTION_HTTP_DCR",
        "CONNECTION_SLACK_OAUTH_U2M_MAPPING",
    }
)

# Credential-state outcomes for :func:`connection_credential_state`.
CREDENTIAL_PRESENT = "present"  # signed in — skip the login
CREDENTIAL_MISSING = "missing"  # confirmed no credential yet — run the login
CREDENTIAL_NO_LOGIN = "no_login_needed"  # not an OAuth-U2M connection — skip
CREDENTIAL_UNKNOWN = (
    "unknown"  # couldn't determine — skip (don't open a browser we're unsure about)
)


def connection_from_url(url: str) -> str | None:
    """Return the connection FQN of an AI Gateway MCP service URL, or ``None``.

    ``https://ws/ai-gateway/mcp-services/system.ai.github`` -> ``system.ai.github``.
    A URL that is not an mcp-services endpoint (or names no service) returns
    ``None`` — only connection-backed services get the login-on-401 treatment.
    """
    marker = url.find(AIGW_MCP_SERVICES_SEGMENT)
    if marker == -1:
        return None
    tail = url[marker + len(AIGW_MCP_SERVICES_SEGMENT) :]
    # Strip any trailing path (``/tools/list``), query, or fragment.
    connection = tail.split("/")[0].split("?")[0].split("#")[0]
    return connection or None


def _strip_connections_prefix(name: str | None) -> str | None:
    """UC returns a connection reference as ``connections/<name>``; the REST path
    wants the bare name."""
    if not name:
        return None
    prefix = "connections/"
    return name[len(prefix) :] if name.startswith(prefix) else name


def connection_credential_state(
    resource_url: str, workspace: str, *, profile: str | None = None
) -> str:
    """Best-effort per-user credential state for a connection-backed MCP service.

    Gates the connect-time login: the proxy should only run ``databricks auth
    login`` (which opens a browser) when the credential is genuinely **missing**,
    not on every session for a service the user already signed in to — otherwise
    N configured servers would each pop a browser every session.

    Uses the same Unity Catalog REST APIs as ``ug mcp login``: resolve the
    service's backing connection, then read the current user's credential
    ``provisioning_info.state``. Returns one of ``CREDENTIAL_{PRESENT,MISSING,
    NO_LOGIN,UNKNOWN}``. Anything but ``MISSING`` means "don't open a browser":
    ``PRESENT``/``NO_LOGIN`` are definitive, and ``UNKNOWN`` (any probe failure)
    deliberately fails safe — ``tools/list`` still works, and a tool call can
    surface the login later — rather than risk a spurious browser.
    """
    full = connection_from_url(resource_url)
    if not full:
        return CREDENTIAL_NO_LOGIN
    try:
        token = get_databricks_token(workspace, profile)
    except Exception:  # noqa: BLE001 - a dead token is reported by _preflight_token, not here
        return CREDENTIAL_UNKNOWN
    user = (_scim_me(workspace, token) or {}).get("userName")
    if not user:
        return CREDENTIAL_UNKNOWN
    prefix = f"https://{workspace_hostname(workspace)}/api/2.1/unity-catalog"
    details, err = _http_get_json(f"{prefix}/mcp-services/{quote(full, safe='')}", token)
    if err is not None or not isinstance(details, dict):
        return CREDENTIAL_UNKNOWN
    source = (details.get("config") or {}).get("source_connection") or {}
    if source.get("securable_kind") not in _OAUTH_U2M_CONNECTION_KINDS:
        return CREDENTIAL_NO_LOGIN
    conn = _strip_connections_prefix(source.get("name"))
    service_id = details.get("id")
    if not conn or not service_id:
        return CREDENTIAL_UNKNOWN
    cred_url = (
        f"{prefix}/connections/{quote(conn, safe='')}/user-credentials/"
        f"{quote(user, safe='')}?dependent.mcp_service.id={quote(str(service_id), safe='')}"
    )
    cred, cred_err = _http_get_json(cred_url, token)
    if cred_err is not None:
        # 404 is the authoritative "no credential yet"; any other error is inconclusive.
        return CREDENTIAL_MISSING if cred_err.startswith("HTTP 404") else CREDENTIAL_UNKNOWN
    if not isinstance(cred, dict):
        return CREDENTIAL_UNKNOWN
    state = ((cred.get("connection_user_credential") or {}).get("provisioning_info") or {}).get(
        "state"
    )
    return CREDENTIAL_PRESENT if state == "ACTIVE" else CREDENTIAL_MISSING


def _cli_supports_resource_flag(login_binary: str) -> bool:
    """Whether ``<login_binary> auth login`` advertises the ``--resource`` flag.

    The connection sign-in needs a Databricks CLI with ``--resource``
    (databricks/cli#6621). An older CLI rejects the flag and the login exits with
    a cryptic parse error, so we check ``--help`` up front to give a clear message
    instead. Fail-open (assume supported) if ``--help`` can't be run — the real
    login attempt will surface any genuine failure."""
    try:
        result = subprocess.run(
            [login_binary, "auth", "login", "--help"],
            check=False,
            timeout=20,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    return "--resource" in f"{result.stdout or ''}{result.stderr or ''}"


def run_connection_login(
    resource_url: str,
    workspace: str,
    *,
    profile: str | None = None,
    login_binary: str = "databricks",
) -> tuple[bool, str]:
    """Run the CLI U2M login with an RFC 8707 resource indicator for this service.

    ``resource_url`` is the MCP service endpoint (also the proxy's upstream URL);
    it is sent as ``--resource`` so a resource-aware ``/oidc`` drives the
    connection's SaaS login before issuing the token. Uses the Databricks CLI's
    own default client, whose loopback redirect is already registered — no
    ``--client-id`` needed.

    The CLI opens the browser to complete the login and prints the authorize URL.
    We route its output to **stderr** (never stdout — that is the proxy's MCP
    JSON-RPC wire), so a coding agent surfaces it in the server's log and the URL
    stays visible when the browser can't open (e.g. a headless remote). Returns
    ``(ok, message)``; on failure ``message`` points at that log.
    """
    connection = connection_from_url(resource_url) or resource_url
    if not _cli_supports_resource_flag(login_binary):
        return False, (
            f"the Databricks CLI ('{login_binary}') has no `--resource` flag, so the "
            f"'{connection}' connection sign-in can't run. Upgrade the CLI "
            "(databricks/cli#6621) and retry."
        )
    argv = [
        login_binary,
        "auth",
        "login",
        "--host",
        workspace.rstrip("/"),
        "--resource",
        resource_url,
    ]
    if profile:
        argv += ["--profile", profile]
    print(
        f"ucode mcp-proxy: '{connection}' needs a one-time connection sign-in. Opening your "
        "browser to complete it — if it doesn't open, use the authorization URL printed below.",
        file=sys.stderr,
        flush=True,
    )
    try:
        # stdout -> stderr: the CLI's prompts and authorize URL reach the agent's
        # MCP log (fd 2) without corrupting this process's stdout (fd 1, the MCP
        # JSON-RPC stream). stdin is closed since the flow is browser-driven.
        result = subprocess.run(
            argv,
            check=False,
            timeout=_LOGIN_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
            stdout=sys.stderr,
            stderr=sys.stderr,
        )
    except OSError as exc:
        return False, f"could not run '{login_binary} auth login': {exc}"
    except subprocess.TimeoutExpired:
        return False, "connection sign-in timed out waiting for the browser flow to complete"
    if result.returncode == 0:
        return True, "signed in"
    return (
        False,
        f"connection sign-in did not complete (CLI exited {result.returncode}; see the log above)",
    )


__all__ = [
    "AIGW_MCP_SERVICES_SEGMENT",
    "CREDENTIAL_MISSING",
    "CREDENTIAL_NO_LOGIN",
    "CREDENTIAL_PRESENT",
    "CREDENTIAL_UNKNOWN",
    "connection_credential_state",
    "connection_from_url",
    "run_connection_login",
]
