"""`ug mcp login`: sign in to the connection-backed AI Gateway MCP services that
the coding agents are configured to use — a `/mcp`-style status list + login.

A connection-backed MCP service (e.g. ``system.ai.github``) only vends its tools
once the user holds a per-user connection credential. This command shows, for the
MCP services ucode has registered for the agents, which are already authenticated
and which still need a sign-in, and runs the sign-in for the ones you pick.

The sign-in is ``databricks auth login --resource <mcp-url>`` (RFC 8707), which a
resource-aware ``/oidc`` routes through the connection's own SaaS login
(``/mcp-service-login``) before minting the token — so it works for **any**
connection-backed MCP service, not just ``system.ai.*``. The per-service
credential status comes from the existing Unity Catalog REST APIs (the same ones
the ``/mcp-service-login`` page uses): the mcp-service's backing connection plus
its per-user credential provisioning state.
"""

from __future__ import annotations

import subprocess
from urllib.parse import quote

from ucode.databricks import (
    _http_get_json,
    _scim_me,
    get_databricks_token,
    workspace_hostname,
)
from ucode.mcp import AIGW_MCP_SERVICES_PATH
from ucode.state import load_state
from ucode.ui import print_note, print_section, print_success, print_warning, spinner

# Connection securable kinds that use per-user OAuth (U2M) credentials — i.e. the
# MCP service needs a connection sign-in. Mirrors the webapp's
# `hasGenericAccessTokenFlowKnownKinds`; anything else needs no per-user login.
_OAUTH_U2M_CONNECTION_KINDS = frozenset(
    {
        "CONNECTION_HTTP_OAUTH_U2M_MAPPING",
        "CONNECTION_HTTP_DCR",
        "CONNECTION_SLACK_OAUTH_U2M_MAPPING",
    }
)

# Per-service login status.
STATUS_AUTHENTICATED = "authenticated"
STATUS_NEEDS_LOGIN = "needs_login"
STATUS_NO_LOGIN = "no_login_needed"
STATUS_UNKNOWN = "unknown"


def mcp_service_full_name_from_url(url: str) -> str | None:
    """Extract the ``<catalog>.<schema>.<service>`` name from an AI Gateway
    mcp-services URL, or ``None`` if it isn't one."""
    if not isinstance(url, str) or AIGW_MCP_SERVICES_PATH not in url:
        return None
    tail = url.split(AIGW_MCP_SERVICES_PATH, 1)[1].strip("/")
    # The service name is the first path segment after the mcp-services prefix.
    name = tail.split("/", 1)[0].split("?", 1)[0]
    return name or None


def _uc_prefix(workspace: str) -> str:
    return f"https://{workspace_hostname(workspace)}/api/2.1/unity-catalog"


def _strip_connections_prefix(name: str | None) -> str | None:
    """UC returns a connection reference as ``connections/<name>``; the REST path
    wants the bare name (mirror of the webapp's ``stripConnectionsPrefix``)."""
    if not name:
        return None
    prefix = "connections/"
    return name[len(prefix) :] if name.startswith(prefix) else name


def mcp_service_login_status(workspace: str, token: str, full_name: str, user_identity: str) -> str:
    """Per-user login status for one connection-backed MCP service.

    Resolves the service's backing connection (any connection, not just
    ``system.ai.*``) and reads the current user's credential provisioning state
    from the Unity Catalog REST APIs:

    1. ``GET /mcp-services/<full_name>`` → the service id + its ``source_connection``
       (name + securable_kind). A non-OAuth-U2M kind never needs a login.
    2. ``GET /connections/<conn>/user-credentials/<user>?dependent.mcp_service.id=<id>``
       → ``connection_user_credential.provisioning_info.state``. ``ACTIVE`` means
       signed in; the endpoint answers **HTTP 404** ("Credential ... is not found
       ... Please login first") when there is no credential yet.
    """
    prefix = _uc_prefix(workspace)
    details, err = _http_get_json(f"{prefix}/mcp-services/{quote(full_name, safe='')}", token)
    if err is not None or not isinstance(details, dict):
        return STATUS_UNKNOWN
    source = (details.get("config") or {}).get("source_connection") or {}
    kind = source.get("securable_kind")
    if kind not in _OAUTH_U2M_CONNECTION_KINDS:
        return STATUS_NO_LOGIN
    conn = _strip_connections_prefix(source.get("name"))
    service_id = details.get("id")
    if not conn or not service_id:
        return STATUS_UNKNOWN

    cred_url = (
        f"{prefix}/connections/{quote(conn, safe='')}/user-credentials/"
        f"{quote(user_identity, safe='')}?dependent.mcp_service.id={quote(str(service_id), safe='')}"
    )
    cred, cred_err = _http_get_json(cred_url, token)
    if cred_err is not None:
        # 404 NOT_FOUND is the authoritative "no credential yet" signal; any other
        # error is inconclusive (don't claim authenticated, don't hard-fail).
        return STATUS_NEEDS_LOGIN if cred_err.startswith("HTTP 404") else STATUS_UNKNOWN
    if not isinstance(cred, dict):
        return STATUS_UNKNOWN
    state = ((cred.get("connection_user_credential") or {}).get("provisioning_info") or {}).get(
        "state"
    )
    return STATUS_AUTHENTICATED if state == "ACTIVE" else STATUS_NEEDS_LOGIN


def run_connection_login(
    mcp_url: str, workspace: str, profile: str | None = None, *, login_binary: str = "databricks"
) -> tuple[bool, str]:
    """Run the Databricks CLI U2M login with an RFC 8707 ``--resource`` indicator
    for one MCP service. A resource-aware ``/oidc`` routes it through the
    connection's own SaaS sign-in before minting the token. The CLI opens the
    browser and prints the authorization URL; this blocks until it completes.

    Returns ``(ok, detail)``. Requires a Databricks CLI with ``--resource``
    (databricks/cli#6621); an older CLI rejects the flag — surfaced as a clear
    failure rather than a cryptic one.
    """
    cmd = [login_binary, "auth", "login", "--host", workspace, "--resource", mcp_url]
    if profile:
        cmd += ["--profile", profile]
    try:
        result = subprocess.run(cmd, check=False, text=True, capture_output=True, timeout=600)
    except FileNotFoundError:
        return False, f"'{login_binary}' not found on PATH"
    except subprocess.TimeoutExpired:
        return False, "login timed out"
    if result.returncode == 0:
        return True, "signed in"
    stderr = (result.stderr or "") + (result.stdout or "")
    if "--resource" in stderr and ("unknown flag" in stderr or "unknown shorthand" in stderr):
        return False, (
            "your Databricks CLI does not support `--resource` (needs databricks/cli#6621). "
            "Upgrade the CLI and retry."
        )
    return False, f"`{login_binary} auth login` failed (exit {result.returncode})"


def _prompt_login_selection(rows: list[tuple[str, str]]) -> list[str] | None:
    """Checklist of connection-backed MCP services with their sign-in status;
    the ones that need a login are pre-checked. ``rows`` is ``(full_name,
    status)``. Returns the selected service names, or ``None`` if cancelled."""
    import questionary

    label = {
        STATUS_AUTHENTICATED: "signed in",
        STATUS_NEEDS_LOGIN: "needs sign-in",
        STATUS_UNKNOWN: "status unknown",
    }
    choices = [
        questionary.Choice(
            title=f"{full}  ({label.get(status, status)})",
            value=full,
            checked=status != STATUS_AUTHENTICATED,
        )
        for full, status in rows
    ]
    selection = questionary.checkbox(
        "Sign in to MCP services (space to toggle, enter to confirm):", choices=choices
    ).ask()
    return None if selection is None else [str(v) for v in selection]


def _connection_mcp_service_entries(
    state: dict, agents: set[str] | None
) -> list[tuple[str, str, list[str]]]:
    """``(full_name, mcp_url, clients)`` for each connection-backed MCP service
    ucode has registered, scoped to ``agents`` when given. Deduped by full name."""
    seen: set[str] = set()
    out: list[tuple[str, str, list[str]]] = []
    for server in state.get("mcp_servers") or []:
        url = server.get("url")
        full = mcp_service_full_name_from_url(url) if isinstance(url, str) else None
        if not full or full in seen:
            continue
        clients = [c for c in (server.get("clients") or []) if isinstance(c, str)]
        if agents is not None and not (set(clients) & agents):
            continue
        seen.add(full)
        out.append((full, url, clients))
    return out


def login_mcp_command(services: set[str] | None = None, agents: set[str] | None = None) -> int:
    """`ug mcp login`: show sign-in status for the agents' connection-backed MCP
    services and sign in to the selected ones. ``--services`` targets specific
    services non-interactively (full ``system.ai.github`` or short ``github``);
    ``--agents`` scopes to those agents. Bare, it shows the picker."""
    state = load_state()
    workspace = state.get("workspace")
    if not workspace:
        raise RuntimeError("Workspace is not configured. Run `ug configure` first.")
    profile = state.get("profile")

    entries = _connection_mcp_service_entries(state, agents)
    if not entries:
        scope = "" if agents is None else f" for {', '.join(sorted(agents))}"
        print_note(f"No connection-backed MCP services are configured{scope}.")
        return 0

    if services is not None:
        entries = [e for e in entries if e[0] in services or e[0].split(".")[-1] in services]
        unknown = services - {e[0] for e in entries} - {e[0].split(".")[-1] for e in entries}
        if unknown:
            print_warning(f"Not configured, skipping: {', '.join(sorted(unknown))}.")
        if not entries:
            print_note("No matching MCP services to sign in to.")
            return 0

    try:
        token = get_databricks_token(workspace, profile)
    except Exception as exc:  # noqa: BLE001 - surface auth trouble as guidance
        raise RuntimeError(
            f"Could not get a Databricks token for {workspace}: {exc}. Run `ug configure` first."
        ) from exc
    user_identity = (_scim_me(workspace, token) or {}).get("userName")
    if not user_identity:
        raise RuntimeError("Could not resolve the current Databricks user identity.")

    print_section("MCP Login")
    with spinner("Checking sign-in status..."):
        status_by_full = {
            full: mcp_service_login_status(workspace, token, full, user_identity)
            for full, _url, _clients in entries
        }

    # Non-interactive (--services): sign in to every targeted service that needs it.
    # Interactive: show the picker (needs-sign-in pre-checked).
    login_needing = [
        (full, url) for full, url, _ in entries if status_by_full[full] == STATUS_NEEDS_LOGIN
    ]
    if services is not None:
        targets = [(f, u) for f, u, _ in entries if status_by_full[f] != STATUS_NO_LOGIN]
    else:
        for full, _url, _ in entries:
            st = status_by_full[full]
            mark = {"authenticated": "✓", "needs_login": "•", "no_login_needed": "-"}.get(st, "?")
            print_note(f"  {mark} {full} ({st.replace('_', ' ')})")
        if not login_needing:
            print_success("All configured MCP services are already signed in.")
            return 0
        selected = _prompt_login_selection([(f, status_by_full[f]) for f, _u, _ in entries])
        if not selected:
            print_note("Nothing selected.")
            return 0
        url_by_full = {f: u for f, u, _ in entries}
        targets = [(f, url_by_full[f]) for f in selected]

    signed_in = 0
    for full, url in targets:
        print_note(f"Signing in to {full}...")
        ok, detail = run_connection_login(url, workspace, profile)
        if ok:
            signed_in += 1
            print_success(f"  {full}: {detail}")
        else:
            print_warning(f"  {full}: {detail}")
    if signed_in:
        print_success(f"Signed in to {signed_in} MCP service(s).")
    return 0


__all__ = [
    "login_mcp_command",
    "mcp_service_login_status",
    "run_connection_login",
    "mcp_service_full_name_from_url",
    "STATUS_AUTHENTICATED",
    "STATUS_NEEDS_LOGIN",
    "STATUS_NO_LOGIN",
    "STATUS_UNKNOWN",
]
