"""Canonical workspace URLs for persistent identity."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def canonical_workspace_origin(workspace: str) -> str:
    """Normalize an HTTP(S) workspace origin for stable IDs and ownership lookup."""
    value = workspace.strip()
    parsed = urlsplit(value if "://" in value else f"https://{value}")
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path.rstrip("/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Workspace must be an HTTP(S) origin URL.")
    authority = parsed.netloc.lower()
    if (parsed.scheme == "https" and parsed.port == 443) or (
        parsed.scheme == "http" and parsed.port == 80
    ):
        authority = authority.rsplit(":", 1)[0]
    return urlunsplit((parsed.scheme, authority, "", "", ""))
