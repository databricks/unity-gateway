"""Check whether a workspace advertises CIMD for native MCP OAuth."""

from __future__ import annotations

import json
import urllib.request

from ucode.ug_headers import ug_headers

MCP_OAUTH_CALLBACK_PORT = 3118


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


__all__ = ["MCP_OAUTH_CALLBACK_PORT", "workspace_supports_cimd"]
