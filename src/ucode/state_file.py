"""Read saved workspace selections without importing agent configuration code."""

from __future__ import annotations

import json
from pathlib import Path

from ucode.workspace_key import split_workspace_key

STATE_VERSION = 3


def read_state_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict) or data.get("state_version") != STATE_VERSION:
        return {"state_version": STATE_VERSION, "current_workspace": None, "workspaces": {}}
    return data


# On a host that serves several workspaces, ``current_workspace`` holds the bare host (what older
# ug versions and integrations read) and ``current_workspace_key`` the ``<host>?w=<id>`` block.
CURRENT_WORKSPACE_KEY = "current_workspace_key"
# Marks the copy of a multi-workspace host's current block kept under the bare host, for
# integrations that look ucode state up by the host alone. ug follows it to that host's current
# workspace and never treats a marked block as that host's own settings.
ALIAS_OF_KEY = "alias_of"


def _split_saved_key(workspaces: object, key: str) -> tuple[str, str | None]:
    """:func:`split_workspace_key` for a saved key.

    An older version saved a pasted URL as its key. A ``<host>?w=<id>`` key whose block exists
    without that ``workspace_id`` (which this version always writes) is that older plain key; a
    key with no block yet selects its workspace.
    """
    host, workspace_id = split_workspace_key(key)
    block = workspaces.get(key) if isinstance(workspaces, dict) else None
    if workspace_id and isinstance(block, dict) and block.get("workspace_id") != workspace_id:
        return key, None
    return host, workspace_id


def current_workspace_key(full: dict) -> str | None:
    """The ``workspaces`` key of the current workspace.

    ``current_workspace_key`` counts only while it matches ``current_workspace``: an older ug that
    switched workspaces updates the bare host alone.
    """
    current = full.get("current_workspace")
    if not isinstance(current, str) or not current:
        return None
    key = full.get(CURRENT_WORKSPACE_KEY)
    if isinstance(key, str) and split_workspace_key(key)[0] == current:
        return key
    workspaces = full.get("workspaces") or {}
    block = workspaces.get(current)
    aliased = block.get(ALIAS_OF_KEY) if isinstance(block, dict) else None
    if isinstance(aliased, str) and aliased in workspaces:
        return aliased
    return current


def current_workspace_state(full: dict) -> dict:
    key = current_workspace_key(full)
    if not key:
        return {}
    workspaces = full.get("workspaces", {})
    state = workspaces.get(key, {}) if isinstance(workspaces, dict) else {}
    state = dict(state) if isinstance(state, dict) else {}
    workspace, workspace_id = _split_saved_key(workspaces, key)
    state["workspace"] = workspace
    if workspace_id:
        state["workspace_id"] = workspace_id
    else:
        state.pop("workspace_id", None)
    state.pop(ALIAS_OF_KEY, None)
    return state
