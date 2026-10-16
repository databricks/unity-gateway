"""Unity Gateway ownership records and Claude Desktop revert policy."""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ucode.agents.desktop import claude_config as desktop_config
from ucode.workspace_urls import canonical_workspace_origin

MANIFEST_FILENAME = "claude-desktop.json"
MANIFEST_VERSION = 1
PROFILE_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://github.com/databricks/unity-gateway/claude-desktop"
)

_MISSING = object()


def _validate_record(value: object, scope: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise desktop_config.ClaudeDesktopConflictError(
            f"Unity Gateway ownership record for {scope} is malformed."
        )
    profile_id = value.get("profile_id")
    if not isinstance(profile_id, str) or not desktop_config.is_uuid(profile_id):
        raise desktop_config.ClaudeDesktopConflictError(
            f"Unity Gateway ownership record for {scope} has an invalid profile ID."
        )
    entry_after = value.get("entry_after")
    if not isinstance(entry_after, dict) or entry_after not in (
        {"id": profile_id, "name": desktop_config.PROFILE_NAME},
        {"id": profile_id, "name": f"{desktop_config.PROFILE_NAME} - {value.get('profile_key')}"},
    ):
        raise desktop_config.ClaudeDesktopConflictError(
            f"Unity Gateway ownership record for {scope} has an invalid metadata entry."
        )
    entry_before = value.get("entry_before")
    if entry_before is not None and (
        not isinstance(entry_before, dict)
        or entry_before.get("id") != profile_id
        or not isinstance(entry_before.get("name"), str)
    ):
        raise desktop_config.ClaudeDesktopConflictError(
            f"Unity Gateway ownership record for {scope} has an invalid metadata baseline."
        )
    profile_before = value.get("profile_before")
    if profile_before is not None and not isinstance(profile_before, dict):
        raise desktop_config.ClaudeDesktopConflictError(
            f"Unity Gateway ownership record for {scope} has an invalid profile baseline."
        )
    owned_after = value.get("owned_after")
    if not isinstance(owned_after, dict):
        profile_after = value.get("profile_after")
        if not isinstance(profile_after, dict):
            raise desktop_config.ClaudeDesktopConflictError(
                f"Unity Gateway ownership record for {scope} is missing owned values."
            )
        owned_after = owned_values(profile_after)
    owned_keys_value = value.get("owned_keys")
    if owned_keys_value is None:
        owned_keys = list(owned_after)
    elif isinstance(owned_keys_value, list) and all(
        isinstance(key, str) for key in owned_keys_value
    ):
        owned_keys = list(owned_keys_value)
    else:
        raise desktop_config.ClaudeDesktopConflictError(
            f"Unity Gateway ownership record for {scope} has invalid owned keys."
        )
    if not set(desktop_config.OWNED_PROFILE_KEYS).issubset(owned_keys) or not set(
        owned_keys
    ).issubset(desktop_config.ALL_PROFILE_KEYS):
        raise desktop_config.ClaudeDesktopConflictError(
            f"Unity Gateway ownership record for {scope} has incomplete owned keys."
        )
    if set(owned_after) != set(owned_keys):
        raise desktop_config.ClaudeDesktopConflictError(
            f"Unity Gateway ownership record for {scope} has incomplete owned values."
        )
    prior_applied_present = value.get("prior_applied_present")
    prior_applied = value.get("prior_applied")
    if not isinstance(prior_applied_present, bool):
        raise desktop_config.ClaudeDesktopConflictError(
            f"Unity Gateway ownership record for {scope} is missing appliedId state."
        )
    if prior_applied_present and (
        not isinstance(prior_applied, str) or not desktop_config.is_uuid(prior_applied)
    ):
        raise desktop_config.ClaudeDesktopConflictError(
            f"Unity Gateway ownership record for {scope} has an invalid appliedId baseline."
        )
    normalized = dict(value)
    normalized["owned_after"] = owned_after
    normalized["owned_keys"] = owned_keys
    normalized["profile_before"] = profile_before
    normalized["entry_after"] = dict(entry_after)
    return normalized


def new_record(
    *,
    directory: Path,
    workspace: str,
    profile_key: str | None,
    profile_id: str,
    profile_before: dict[str, object] | None,
    profile_after: dict[str, object],
    entry_after: Mapping[str, object],
    entry_before: desktop_config.ProfileEntry | None,
    owned_keys: Sequence[str],
    prior_applied_present: bool,
    prior_applied: object,
) -> dict[str, object]:
    return {
        "directory": str(directory),
        "workspace": workspace,
        "profile_key": profile_key or "default",
        "profile_id": profile_id,
        "profile_before": copy.deepcopy(profile_before),
        "profile_after": copy.deepcopy(profile_after),
        "owned_keys": list(owned_keys),
        "owned_after": owned_values(profile_after, owned_keys),
        "entry_after": copy.deepcopy(entry_after),
        "entry_before": copy.deepcopy(entry_before),
        "prior_applied_present": prior_applied_present,
        "prior_applied": prior_applied,
    }


def read_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": MANIFEST_VERSION, "profiles": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise desktop_config.ClaudeDesktopConflictError(
            f"Unity Gateway Claude Desktop ownership state at {path} is malformed; refusing to overwrite it."
        ) from exc
    if not isinstance(value, dict) or value.get("version") != MANIFEST_VERSION:
        raise desktop_config.ClaudeDesktopConflictError(
            f"Unity Gateway Claude Desktop ownership state at {path} has an unsupported version."
        )
    profiles = value.get("profiles")
    if not isinstance(profiles, dict):
        raise desktop_config.ClaudeDesktopConflictError(
            f"Unity Gateway Claude Desktop ownership state at {path} is malformed."
        )
    return value


def scope_key(directory: Path, profile_id: str) -> str:
    return json.dumps(
        [str(directory), profile_id],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def find_workspace_record(
    manifest: Mapping[str, Any], directory: Path, workspace: str, *, profile_id: str | None = None
) -> tuple[str | None, dict[str, Any] | None]:
    matches = []
    for scope, value in manifest["profiles"].items():
        if not isinstance(value, dict) or not isinstance(value.get("directory"), str):
            raise desktop_config.ClaudeDesktopConflictError(
                f"Unity Gateway ownership record for {scope} is malformed."
            )
        recorded_workspace = value.get("workspace")
        if not isinstance(recorded_workspace, str):
            raise desktop_config.ClaudeDesktopConflictError(
                f"Unity Gateway ownership record for {scope} is malformed."
            )
        if (
            value["directory"] != str(directory)
            or canonical_workspace_origin(recorded_workspace) != workspace
        ):
            continue
        matches.append((scope, value))
    managed_id = profile_id or str(uuid.uuid5(PROFILE_NAMESPACE, workspace))
    managed_matches = [item for item in matches if item[1].get("profile_id") == managed_id]
    matches = managed_matches if profile_id is not None or managed_matches else matches
    if len(matches) > 1:
        raise desktop_config.ClaudeDesktopConflictError(
            "Multiple UG-owned Claude Desktop profiles exist for this workspace. "
            "Resolve their revert records before retrying; no profiles were changed."
        )
    if not matches:
        return None, None
    scope, value = matches[0]
    return scope, _validate_record(value, scope)


def merge_owned_profile(
    profile: Mapping[str, object], rendered: Mapping[str, object]
) -> dict[str, object]:
    merged = copy.deepcopy(dict(profile))
    for key in desktop_config.OWNED_PROFILE_KEYS:
        merged[key] = copy.deepcopy(rendered[key])
    for key in desktop_config.OPTIONAL_PROFILE_KEYS:
        if key in rendered:
            merged[key] = copy.deepcopy(rendered[key])
    return merged


def owned_keys_for_rendered(
    rendered: Mapping[str, object], *, previous: Sequence[str] | None = None
) -> list[str]:
    keys = list(previous) if previous is not None else list(desktop_config.OWNED_PROFILE_KEYS)
    for key in desktop_config.OPTIONAL_PROFILE_KEYS:
        if key in rendered and key not in keys:
            keys.append(key)
    return keys


def owned_values(
    profile: Mapping[str, object], keys: Sequence[str] = desktop_config.OWNED_PROFILE_KEYS
) -> dict[str, object]:
    return {key: copy.deepcopy(profile[key]) for key in keys if key in profile}


def apply_metadata(
    metadata: desktop_config.Metadata, profile_id: str, entry: desktop_config.ProfileEntry
) -> desktop_config.Metadata:
    result = copy.deepcopy(metadata)
    entries = result["entries"]
    for index, item in enumerate(entries):
        if item["id"] == profile_id:
            entries[index] = copy.deepcopy(entry)
            break
    else:
        entries.append(copy.deepcopy(entry))
    result["appliedId"] = profile_id
    return result


def revert_profile(
    profile: dict[str, object] | None,
    record: Mapping[str, Any],
    path: Path,
) -> tuple[dict[str, object] | None, bool, bool]:
    if profile is None:
        return None, False, False
    expected = record["owned_after"]
    baseline = record["profile_before"]
    if not isinstance(expected, dict) or (baseline is not None and not isinstance(baseline, dict)):
        raise desktop_config.ClaudeDesktopConflictError(
            f"Unity Gateway ownership record for {path} is malformed."
        )
    if baseline is None and (
        set(profile) != set(expected)
        or any(profile.get(key, _MISSING) != value for key, value in expected.items())
    ):
        return profile, False, True
    reverted = copy.deepcopy(profile)
    drifted = False
    for key, expected_value in expected.items():
        current_value = profile.get(key, _MISSING)
        if current_value != expected_value:
            drifted = True
            continue
        if baseline is not None and key in baseline:
            reverted[key] = copy.deepcopy(baseline[key])
        else:
            reverted.pop(key, None)
    if baseline is None and not reverted:
        return None, True, drifted
    return reverted, reverted != profile, drifted


def revert_metadata(
    metadata: desktop_config.Metadata,
    record: Mapping[str, Any],
    *,
    directory: Path,
    keep_entry: bool,
) -> tuple[desktop_config.Metadata, bool]:
    result = copy.deepcopy(metadata)
    profile_id = record["profile_id"]
    expected_entry = record["entry_after"]
    entries = result["entries"]
    entry_before = record.get("entry_before")
    if entry_before is not None:
        for index, entry in enumerate(entries):
            if entry == expected_entry:
                entries[index] = copy.deepcopy(entry_before)
                break
    elif not keep_entry:
        result["entries"] = [entry for entry in entries if entry != expected_entry]
    if result.get("appliedId") == profile_id:
        prior = record["prior_applied"]
        prior_exists = (
            isinstance(prior, str)
            and any(item.get("id") == prior for item in result["entries"])
            and desktop_config.profile_path(directory, prior).is_file()
        )
        if record["prior_applied_present"] and prior_exists:
            result["appliedId"] = record["prior_applied"]
        elif not keep_entry:
            result.pop("appliedId", None)
    return result, result != metadata
