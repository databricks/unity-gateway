"""Discovery of the OAuth client used for direct-HTTP MCP registration.

A connection-backed AI Gateway MCP service (e.g. ``system.ai.github``) needs a
per-user connection login before its tools can be called. When a coding agent
registers the service as a **direct HTTP** MCP server, the agent itself drives
that login via OAuth against the workspace ``/oidc`` — and `/oidc` has no dynamic
client registration, so the agent must present a **pre-registered public client**.

Claude Code selects its own CIMD URL when the workspace advertises support.
Codex CLI and Cursor still need pinned, published URLs; we probe and cache those
per workspace.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode

from ucode.config_io import APP_DIR, read_json_safe, write_json_file
from ucode.ug_headers import ug_headers

# Codex CLI's published CIMD document is separate from ChatGPT's hosted client.
# It supports Codex's loopback OAuth callback rather than ChatGPT's hosted redirect.
CODEX_CIMD_CLIENT_ID = "https://chatgpt.com/oauth/codex/client.json"

MCP_OAUTH_CALLBACK_PORT = 3118

# Cursor's CIMD document includes its desktop loopback callback at
# `http://localhost:8787/callback`, so the same published client ID works in
# Cursor's `mcp.json` `auth.CLIENT_ID` field.
CURSOR_OAUTH_CLIENT_ID = "https://cursor.com/oauth/mcp-client.json"

# A published app stays available, but an absent app may be published at any time.
# Cache positive results only, so the next run detects a newly published app.
_CACHE_PATH = APP_DIR / "oauth_client_cache.json"
_CACHE_TTL_SECONDS = 7 * 24 * 3600


def workspace_supports_cimd(workspace: str) -> bool:
    """Whether this workspace advertises public-client CIMD for agent-driven OAuth."""
    request = urllib.request.Request(
        f"{workspace.rstrip('/')}/oidc/.well-known/oauth-authorization-server",
        headers=ug_headers(),
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            metadata = json.load(response)
    except (OSError, ValueError):
        return False
    if not isinstance(metadata, dict):
        return False
    methods = metadata.get("token_endpoint_auth_methods_supported")
    return (
        metadata.get("client_id_metadata_document_supported") is True
        and isinstance(methods, list)
        and "none" in methods
    )


def _probe_oauth_client(workspace: str, client_id: str) -> bool | None:
    """Whether ``client_id`` is a registered OAuth app on the workspace's ``/oidc``:
    ``True`` (registered), ``False`` (not registered), or ``None`` (inconclusive).

    Back-channel and unauthenticated: POST a throwaway ``authorization_code`` grant
    to ``/oidc/v1/token``. An **unknown** client fails client authentication (HTTP
    401 ``invalid_client`` -> ``False``); a **known** client gets past that to a
    grant error (HTTP 400 ``invalid_request`` — "Invalid authorization code" ->
    ``True``); the dummy code always fails, harmlessly. Any other outcome — a
    transient 429/5xx, a 404/redirect, a network error, or an unexpected 2xx — is
    **inconclusive** (``None``): we must not read "not 401" as "registered", or an
    incident/rate-limit would flip every workspace to the direct-HTTP path. This is
    the only user-level check available — the authorize endpoint redirects to SSO
    before validating the client, and the published-app API needs account-admin."""
    body = urlencode(
        {
            "grant_type": "authorization_code",
            "code": "ucode-probe-not-a-real-code",
            "redirect_uri": f"http://localhost:{MCP_OAUTH_CALLBACK_PORT}/callback",
            "client_id": client_id,
            # `/oidc` rejects a code_verifier shorter than 43 chars *before* it
            # validates the client, so pad past that to reach the client check.
            "code_verifier": "u" * 43,
        }
    ).encode("ascii")
    request = urllib.request.Request(
        f"{workspace.rstrip('/')}/oidc/v1/token",
        data=body,
        method="POST",
        headers={**ug_headers(), "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        urllib.request.urlopen(request, timeout=10)  # noqa: S310 - fixed https workspace URL
        return None  # a 2xx for a dummy code is unexpected; don't conclude either way
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return False  # invalid_client => the app is not registered
        if exc.code == 400:
            return True  # known client, rejected only on the dummy code => registered
        return None  # 429/5xx/404/redirect/etc => inconclusive
    except OSError:
        return None  # network failure => inconclusive


def _read_cache() -> dict:
    return read_json_safe(_CACHE_PATH)


def _write_cache(cache: dict) -> None:
    # write_json_file creates APP_DIR if missing (like every other APP_DIR writer);
    # best-effort — a write failure just means we re-probe next time.
    try:
        write_json_file(_CACHE_PATH, cache)
    except OSError:
        pass


def oauth_client_available(workspace: str, client_id: str) -> bool:
    """Whether the workspace has ``client_id`` as a registered OAuth app, cached
    per (workspace, client_id).

    Positive results are cached in ``APP_DIR`` with a weekly TTL. Negative and
    inconclusive results are re-probed on the next run so a newly published app
    becomes usable without waiting for a cache expiration."""
    ws = workspace.rstrip("/")
    cache = _read_cache()
    entry = cache.get(ws, {}).get(client_id)
    if (
        entry
        and entry.get("available")
        and (time.time() - entry.get("checked_at", 0)) < _CACHE_TTL_SECONDS
    ):
        return True
    available = _probe_oauth_client(ws, client_id)
    if available is None:
        # Inconclusive probe (transient status / network error): fall back to the
        # stdio proxy and do NOT cache, so a transient failure isn't sticky for the
        # TTL — we re-probe on the next run.
        return False
    if available:
        cache.setdefault(ws, {})[client_id] = {
            "available": True,
            "checked_at": time.time(),
        }
        _write_cache(cache)
    return available


def first_available_oauth_client(workspace: str, client_ids: tuple[str, ...]) -> str | None:
    """Choose the first published client for this workspace, preserving fallback order."""
    return next(
        (client_id for client_id in client_ids if oauth_client_available(workspace, client_id)),
        None,
    )


__all__ = [
    "CODEX_CIMD_CLIENT_ID",
    "CURSOR_OAUTH_CLIENT_ID",
    "MCP_OAUTH_CALLBACK_PORT",
    "oauth_client_available",
    "first_available_oauth_client",
    "workspace_supports_cimd",
]
