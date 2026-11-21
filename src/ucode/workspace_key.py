"""Workspace state keys shared by configuration and lightweight helper processes."""

from __future__ import annotations

import re

# A saved key also accepts a UUID id, so a key written by a version that supports
# disaster-recovery stable connection ids still round-trips here.
_KEY_PATTERN = re.compile(
    r"^(https?://[^/?#]+)\?w=([0-9]+|[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})$"
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
