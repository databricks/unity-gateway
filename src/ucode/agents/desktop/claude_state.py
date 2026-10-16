"""Shared Claude Desktop metadata and previous-selection bookkeeping."""

from __future__ import annotations

import copy
from pathlib import Path

from ucode.agents.desktop import claude_config as desktop_config

STATE_FILENAME = "claude-desktop.json"


def previous_selection(contents: bytes | None, path: Path) -> str | None:
    state = (
        desktop_config.parse_object(contents, path, "Unity Gateway Desktop selection state")
        if contents is not None
        else {}
    )
    prior = state.get("prior_applied")
    if prior is not None and (not isinstance(prior, str) or not desktop_config.is_uuid(prior)):
        raise desktop_config.ClaudeDesktopConflictError(
            f"Unity Gateway Desktop selection state at {path} has an invalid prior_applied ID."
        )
    return prior


def apply_metadata(
    metadata: desktop_config.Metadata, *, profile_key: str | None
) -> desktop_config.Metadata:
    result = copy.deepcopy(metadata)
    entry: desktop_config.ProfileEntry = {
        "id": desktop_config.PROFILE_ID,
        "name": f"{desktop_config.PROFILE_NAME} - {profile_key}"
        if profile_key
        else desktop_config.PROFILE_NAME,
    }
    entries = result["entries"]
    for index, item in enumerate(entries):
        if item["id"] == desktop_config.PROFILE_ID:
            entries[index] = entry
            break
    else:
        entries.append(entry)
    result["appliedId"] = desktop_config.PROFILE_ID
    return result


def revert_metadata(
    metadata: desktop_config.Metadata, *, prior_applied: str | None, directory: Path
) -> desktop_config.Metadata:
    result = copy.deepcopy(metadata)
    result["entries"] = [
        entry for entry in result["entries"] if entry["id"] != desktop_config.PROFILE_ID
    ]
    if result.get("appliedId") == desktop_config.PROFILE_ID:
        result.pop("appliedId")
        if (
            prior_applied is not None
            and any(entry["id"] == prior_applied for entry in result["entries"])
            and (directory / f"{prior_applied}.json").is_file()
        ):
            result["appliedId"] = prior_applied
    return result
