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

from urllib.parse import quote

from rich.table import Table

from ucode.databricks import (
    _http_get_json,
    _scim_me,
    get_databricks_token,
    workspace_hostname,
)
from ucode.mcp import configured_mcp_servers_by_name
from ucode.mcp_connection_login import connection_from_url, run_connection_login
from ucode.state import load_state
from ucode.ui import (
    console,
    muted,
    print_heading,
    print_kv,
    print_note,
    print_section,
    print_success,
    print_warning,
    spinner,
    status_badge,
)

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


def _uc_prefix(workspace: str) -> str:
    return f"https://{workspace_hostname(workspace)}/api/2.1/unity-catalog"


def _strip_connections_prefix(name: str | None) -> str | None:
    """UC returns a connection reference as ``connections/<name>``; the REST path
    wants the bare name (mirror of the webapp's ``stripConnectionsPrefix``)."""
    if not name:
        return None
    prefix = "connections/"
    return name[len(prefix) :] if name.startswith(prefix) else name


def mcp_service_login_status(workspace: str, token: str, full_name: str, user_id: str) -> str:
    """Per-user login status for one connection-backed MCP service.

    Resolves the service's backing connection (any connection, not just
    ``system.ai.*``) and reads the current user's credential provisioning state
    from the Unity Catalog REST APIs:

    1. ``GET /mcp-services/<full_name>`` → the service id + its ``source_connection``
       (name + securable_kind). A non-OAuth-U2M kind never needs a login.
    2. ``GET /connections/<conn>/user-credentials/<user>?dependent.mcp_service.id=<id>``
       → ``connection_user_credential.provisioning_info.state``. ``ACTIVE`` means
       signed in; the endpoint answers **HTTP 404** ("Credential ... is not found
       ... Please login first") when there is no credential yet. ``<user>`` is the
       numeric workspace user id (the ``connection_user_credential`` key), not the
       email — matching the webapp, which keys on ``userId``.
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
        f"{quote(str(user_id), safe='')}?dependent.mcp_service.id={quote(str(service_id), safe='')}"
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


def _login_status_markup(status: str) -> str:
    """Colored sign-in status token, using the same `status_badge` styling as the STATUS
    column of `ug mcp list`."""
    return {
        STATUS_AUTHENTICATED: status_badge("signed in", "ok"),
        STATUS_NEEDS_LOGIN: status_badge("needs sign-in", "warn"),
        STATUS_NO_LOGIN: muted("no sign-in needed"),
    }.get(status, status_badge("status unknown", "warn"))


def _connection_mcp_service_entries(
    state: dict, agents: set[str] | None
) -> list[tuple[str, str, list[str], bool]]:
    """``(full_name, mcp_url, clients, managed)`` for each connection-backed MCP service ug has
    configured (developer- or workspace-managed), scoped to ``agents`` when given.

    Uses the shared `configured_mcp_servers_by_name` enumeration so the set matches `ug mcp list`, then
    keeps only the AI Gateway mcp-services (the ones that can have a per-user connection login)."""
    out: list[tuple[str, str, list[str], bool]] = []
    for entry in configured_mcp_servers_by_name(state, agents).values():
        url = entry["server"].get("url")
        full = connection_from_url(url) if isinstance(url, str) else None
        if not full:
            continue
        out.append((full, url, entry["clients"], entry["managed"]))
    return out


def login_mcp_command(names: set[str] | None = None, agents: set[str] | None = None) -> int:
    """`ug mcp login`: show sign-in status for the agents' connection-backed MCP
    services and sign in to the selected ones. ``--names`` targets specific
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

    if names is not None:
        entries = [e for e in entries if e[0] in names or e[0].split(".")[-1] in names]
        unknown = names - {e[0] for e in entries} - {e[0].split(".")[-1] for e in entries}
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
    # The connection user-credentials API keys on the numeric workspace user id
    # (the `connection_user_credential.user_id`), not the email — same as the webapp.
    user_id = (_scim_me(workspace, token) or {}).get("id")
    if not user_id:
        raise RuntimeError("Could not resolve the current Databricks user id.")

    print_section("MCP login")
    print_kv("Workspace", workspace)
    with spinner("Checking sign-in status..."):
        status_by_full = {
            full: mcp_service_login_status(workspace, token, full, user_id)
            for full, _url, _clients, _managed in entries
        }

    url_by_full = {full: url for full, url, _clients, _managed in entries}
    # "Pending" = still needs a sign-in OR its status couldn't be determined. UNKNOWN is included
    # so an unreachable status API doesn't masquerade as "already signed in" (and so the picker and
    # the non-interactive path still act on it), rather than being silently skipped.
    pending = [
        full
        for full in url_by_full
        if status_by_full[full] in (STATUS_NEEDS_LOGIN, STATUS_UNKNOWN)
    ]

    # Non-interactive (--names): sign in only to targeted services that still need it — never
    # re-run `databricks auth login` (which blocks on a browser) for one that's already signed in.
    # Interactive: render the per-service status (same Table style as `ug mcp list`), then a picker.
    if names is not None:
        targets = [(f, url_by_full[f]) for f in pending]
    else:
        print_heading("Connection-backed MCP services")
        table = Table(box=None, pad_edge=False, header_style="bold")
        table.add_column("MCP SERVICE", no_wrap=True)
        table.add_column("AGENTS")
        table.add_column("SIGN-IN")
        for full, _url, clients, managed in entries:
            name = full + (" [magenta](managed)[/magenta]" if managed else "")
            table.add_row(name, ", ".join(clients), _login_status_markup(status_by_full[full]))
        console.print(table)
        if not pending:
            print_success("All configured MCP services are already signed in.")
            return 0
        # Only services that can take a sign-in reach the picker; a NO_LOGIN service (its connection
        # needs no per-user login) would otherwise show pre-checked and try to run a pointless login.
        selectable = [
            (full, status_by_full[full])
            for full, _u, _c, _m in entries
            if status_by_full[full] != STATUS_NO_LOGIN
        ]
        selected = _prompt_login_selection(selectable)
        if not selected:
            print_note("Nothing selected.")
            return 0
        targets = [(f, url_by_full[f]) for f in selected]

    signed_in = 0
    for full, url in targets:
        # `run_connection_login` announces the sign-in on stderr (and prints the
        # authorize URL there), so we don't repeat a "Signing in..." note here.
        ok, detail = run_connection_login(url, workspace, profile=profile)
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
    "STATUS_AUTHENTICATED",
    "STATUS_NEEDS_LOGIN",
    "STATUS_NO_LOGIN",
    "STATUS_UNKNOWN",
]
