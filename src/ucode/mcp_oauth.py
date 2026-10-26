"""Check whether a workspace advertises CIMD for native MCP OAuth."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from functools import lru_cache
from urllib.parse import urlencode

from ucode.ug_headers import ug_headers
from ucode.workspace import workspace_headers

MCP_OAUTH_CALLBACK_PORT = 3118

# Published CIMD documents for native MCP OAuth. Pin all three client IDs until
# agent-selected client discovery is supported across the workspace rollout.
CLAUDE_CODE_CIMD_CLIENT_ID = "https://claude.ai/oauth/claude-code-client-metadata"
CODEX_CIMD_CLIENT_ID = "https://chatgpt.com/oauth/codex/client.json"
CURSOR_CIMD_CLIENT_ID = "https://cursor.com/oauth/mcp-client.json"


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


@lru_cache(maxsize=32)
def oauth_client_available(workspace: str, client_id: str) -> bool:
    """Check whether /oidc recognizes a pinned client, without authenticating.

    CIMD metadata advertises protocol support, not which published apps exist.
    A deliberately invalid code yields 400 for a known client and 401 for an
    unknown one. Any other result is inconclusive and keeps the CLI proxy.
    Cache only within this process, so a later `ug mcp add` sees new app rollouts.
    """
    body = urlencode(
        {
            "grant_type": "authorization_code",
            "code": "ug-probe-invalid-code",
            "redirect_uri": f"http://localhost:{MCP_OAUTH_CALLBACK_PORT}/callback",
            "client_id": client_id,
            "code_verifier": "u" * 43,
        }
    ).encode("ascii")
    request = urllib.request.Request(
        f"{workspace.rstrip('/')}/oidc/v1/token",
        data=body,
        headers={
            **ug_headers(),
            **workspace_headers(workspace),
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        urllib.request.urlopen(request, timeout=10)  # noqa: S310
    except urllib.error.HTTPError as exc:
        return exc.code == 400
    except OSError:
        return False
    return False


__all__ = [
    "CLAUDE_CODE_CIMD_CLIENT_ID",
    "CODEX_CIMD_CLIENT_ID",
    "CURSOR_CIMD_CLIENT_ID",
    "MCP_OAUTH_CALLBACK_PORT",
    "oauth_client_available",
    "workspace_supports_cimd",
]
