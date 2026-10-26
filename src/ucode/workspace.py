"""Workspace identity, including hosts that serve several workspaces (account custom URLs)."""

from __future__ import annotations

import http.client
import json
import re
import uuid
from dataclasses import dataclass
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import parse_qs, urlparse

from ucode.ui import normalize_workspace_url, print_note

UNIFIED_HOST_TYPE = "unified"
WORKSPACE_SELECTOR_PARAMS = ("w", "o", "workspace_id")
# A numeric id at or above 2^53 makes the server answer an empty 503.
_MAX_WORKSPACE_ID = 2**53
_HOST_TYPE_TIMEOUT_SECONDS = 3
# A saved key also accepts a UUID id, so a key written by a version that supports
# disaster-recovery stable connection ids still round-trips here.
_KEY_PATTERN = re.compile(
    r"^(https?://[^/?#]+)\?w=([0-9]+|[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})$"
)
_host_types: dict[str, str | None] = {}
_DIGITS = re.compile(r"[0-9]+")


@dataclass(frozen=True)
class WorkspaceInput:
    host: str
    workspace_id: str | None
    extra_parts: bool


def split_workspace_url(raw: str) -> WorkspaceInput:
    """Split a workspace URL into its origin and its workspace selector, if any.

    Accepts the selector forms the Databricks CLI accepts (``?w=``, ``?o=``, ``?workspace_id=``).
    Any other path, query or fragment is reported in ``extra_parts`` and dropped.
    """
    try:
        url = normalize_workspace_url(raw)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError(f"Invalid workspace URL: {raw}")
    query = parse_qs(parsed.query, keep_blank_values=True)
    selectors = {
        value.strip()
        for param in WORKSPACE_SELECTOR_PARAMS
        for value in query.get(param, [])
        if value.strip()
    }
    if len(selectors) > 1:
        raise RuntimeError(
            f"{raw} selects more than one workspace: {', '.join(sorted(selectors))}."
        )
    extra_parts = (
        parsed.path not in {"", "/"}
        or bool(parsed.fragment)
        or any(param not in WORKSPACE_SELECTOR_PARAMS for param in query)
    )
    return WorkspaceInput(
        host=f"{parsed.scheme}://{parsed.netloc}",
        workspace_id=next(iter(selectors), None),
        extra_parts=extra_parts,
    )


def workspace_key(host: str, workspace_id: str | None) -> str:
    """The ``state.json`` key: the bare host, plus ``?w=<id>`` on a multi-workspace host."""
    return f"{host}?w={workspace_id}" if workspace_id else host


def split_workspace_key(key: str) -> tuple[str, str | None]:
    """Inverse of :func:`workspace_key`; any other string is a bare host."""
    match = _KEY_PATTERN.match(key)
    if match:
        return match.group(1), match.group(2)
    return key, None


def state_workspace_key(state: dict) -> str | None:
    workspace = state.get("workspace")
    if not isinstance(workspace, str) or not workspace:
        return None
    workspace_id = state.get("workspace_id")
    return workspace_key(workspace, workspace_id if isinstance(workspace_id, str) else None)


def validate_workspace_id(workspace_id: str) -> str:
    value = workspace_id.strip()
    try:
        uuid.UUID(value)
    except ValueError:
        pass
    else:
        raise RuntimeError(
            f"Workspace id {value} is a disaster-recovery stable connection id, which ug does "
            "not support yet. Use the numeric workspace id instead."
        )
    if not _DIGITS.fullmatch(value) or int(value) >= _MAX_WORKSPACE_ID:
        raise RuntimeError(f"Workspace id {value} is not a valid numeric workspace id.")
    return str(int(value))


class HostUnreachableError(RuntimeError):
    pass


def host_type(host: str) -> str | None:
    """The host's ``host_type`` from its unauthenticated ``/.well-known/databricks-config``.

    Fetched without a workspace selector, which would replace the host-level answer. ``None`` when
    the host answers without one (an older or single-workspace host). Raises
    :class:`HostUnreachableError` on no answer or a server error; that result isn't cached.
    """
    if host in _host_types:
        return _host_types[host]
    request = urllib_request.Request(
        f"{host}/.well-known/databricks-config", headers={"Accept": "application/json"}
    )
    result: str | None = None
    try:
        with urllib_request.urlopen(request, timeout=_HOST_TYPE_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
        value = payload.get("host_type") if isinstance(payload, dict) else None
        result = value.lower() if isinstance(value, str) else None
    except urllib_error.HTTPError as exc:
        if exc.code >= 500:
            raise HostUnreachableError(f"{host} answered HTTP {exc.code}.") from exc
        result = None
    except ValueError:
        result = None
    except (urllib_error.URLError, http.client.HTTPException, OSError) as exc:
        reason = exc.reason if isinstance(exc, urllib_error.URLError) else exc
        raise HostUnreachableError(f"{host} did not answer ({reason}).") from exc
    _host_types[host] = result
    return result


def resolve_workspace(
    raw: str,
    workspace_id: str | None = None,
    *,
    explicit_id: bool = True,
) -> str:
    """Resolve a workspace URL plus an optional workspace id into its ``state.json`` key.

    The id only counts on a host that serves several workspaces. Any other URL resolves to the key
    earlier versions saved for it, so existing ``state.json`` entries and policies still match.
    ``explicit_id`` is False for an id read from a Databricks CLI profile, which single-workspace
    profiles carry too.
    """
    parsed = split_workspace_url(raw)
    flag_id = workspace_id.strip() if workspace_id and workspace_id.strip() else None
    if flag_id and parsed.workspace_id and flag_id != parsed.workspace_id:
        raise RuntimeError(
            f"--workspace-id {flag_id} conflicts with the workspace {parsed.workspace_id} "
            f"selected in {raw}."
        )
    selected = flag_id or parsed.workspace_id
    legacy_key = normalize_workspace_url(raw)
    if selected is None:
        return legacy_key
    try:
        kind = host_type(parsed.host)
    except HostUnreachableError as exc:
        raise RuntimeError(
            f"Could not check whether {parsed.host} serves several workspaces: {exc} "
            "Check your network and retry."
        ) from exc
    if kind == UNIFIED_HOST_TYPE:
        if parsed.extra_parts:
            print_note(f"Using {parsed.host}; the rest of {raw} is not part of the workspace URL.")
        return workspace_key(parsed.host, validate_workspace_id(selected))
    if explicit_id or parsed.workspace_id:
        print_note(f"{parsed.host} serves a single workspace, so the workspace id is not needed.")
    return legacy_key


# Org-Id routes in every region. Workspace-Id is the newer header and the only one that accepts a
# disaster-recovery connection id; both may be sent with the same value.
SELECTOR_HEADER_NAMES = ("X-Databricks-Org-Id", "X-Databricks-Workspace-Id")
# Carries the workspace to a ug helper an agent starts with an environment instead of arguments.
WORKSPACE_ID_ENV_VAR = "UG_WORKSPACE_ID"
_active_host: str | None = None
_active_workspace_id: str | None = None
_pinned = False


def selector_headers(workspace_id: str | None) -> dict[str, str]:
    if not workspace_id:
        return {}
    return dict.fromkeys(SELECTOR_HEADER_NAMES, workspace_id)


def activate(host: str | None, workspace_id: str | None) -> None:
    """Select the workspace whose host gets the selector headers in this process.

    A module global rather than a context variable: worker threads must see it.
    """
    global _active_host, _active_workspace_id
    _active_host = split_workspace_url(host).host if host and workspace_id else None
    _active_workspace_id = workspace_id if _active_host else None


def pin(host: str, workspace_id: str) -> None:
    """Activate the workspace a helper was started for; loading saved state no longer changes it."""
    global _pinned
    activate(host, workspace_id)
    _pinned = True


def activate_state(state: dict) -> None:
    if _pinned:
        return
    workspace = state.get("workspace")
    workspace_id = state.get("workspace_id")
    activate(
        workspace if isinstance(workspace, str) else None,
        workspace_id if isinstance(workspace_id, str) else None,
    )


def active_workspace_id() -> str | None:
    return _active_workspace_id


def workspace_headers(url: str | None = None) -> dict[str, str]:
    """The selector headers for ``url``, or for the active workspace's host when ``url`` is None.

    Empty unless the active workspace is on a multi-workspace host and ``url`` is on that host, so
    nothing is sent to single-workspace hosts, app URLs, or other services.
    """
    if not _active_host:
        return {}
    if url is not None:
        parsed = urlparse(url)
        if f"{parsed.scheme}://{parsed.netloc}" != _active_host:
            return {}
    return selector_headers(_active_workspace_id)


class WorkspaceSelectionReason(str):
    """A failure reason caused by the workspace selection, which callers report as is."""


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def signin_redirect_reason(url: str) -> WorkspaceSelectionReason:
    """Why a request to ``url`` was answered with the sign-in page instead of the API."""
    host = _origin(url)
    selected = workspace_headers(url).get(SELECTOR_HEADER_NAMES[0])
    if selected:
        return WorkspaceSelectionReason(
            f"{host} did not route the request to workspace {selected}; check the workspace id."
        )
    if is_unified_host(host):
        return WorkspaceSelectionReason(
            f"{host} serves several workspaces and no workspace was selected. Pass "
            f"`--workspace '{host}?w=<workspace id>'` or a `--profile` with a workspace_id."
        )
    return WorkspaceSelectionReason(
        f"{host} redirected to sign-in: the token was rejected or needs a fresh login."
    )


def workspace_failure_reason(url: str, status: int, body: str) -> WorkspaceSelectionReason | None:
    """A clearer reason for a failure caused by the workspace selection, else None."""
    if status == 403 and "account admin" in body and is_unified_host(_origin(url)):
        return signin_redirect_reason(url)
    selected = workspace_headers(url).get(SELECTOR_HEADER_NAMES[0])
    if selected and status == 400 and "Invalid Token" in body:
        return WorkspaceSelectionReason(
            f"{_origin(url)} rejected the token for workspace {selected}: the token is invalid, "
            "or the workspace belongs to another account."
        )
    return None


def response_workspace_mismatch(
    url: str, response_org_id: str | None
) -> WorkspaceSelectionReason | None:
    """A failure reason when the host answered for a different workspace than the one selected."""
    selected = workspace_headers(url).get(SELECTOR_HEADER_NAMES[0])
    if not selected or not response_org_id or response_org_id == selected:
        return None
    return WorkspaceSelectionReason(
        f"{_origin(url)} answered for workspace {response_org_id}, not the selected {selected}."
    )


def workspace_id_args(host: str) -> list[str]:
    """``--workspace-id <id>`` for a ug helper bound to ``host``, when the selection applies there."""
    workspace_id = workspace_headers(host).get(SELECTOR_HEADER_NAMES[0])
    return ["--workspace-id", workspace_id] if workspace_id else []
