"""Claude Desktop third-party profile configuration.

Claude Desktop keeps its third-party provider profiles outside the normal Claude Code
configuration. This module owns only the profile entry it creates, plus the ``appliedId``
selection while that selection still points at the Unity Gateway profile. Optional custom headers
and the observed ``alwaysStartWithDefaultModel`` boolean are written only when supplied.

The native path is supported only on macOS.  ``directory=`` is an explicit filesystem injection
for tests and does not imply support for another native platform.
"""

from __future__ import annotations

import copy
import json
import ntpath
import os
import sys
import tempfile
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ucode.config_io import APP_DIR, atomic_write_json
from ucode.os_compatibility.file_lock_cross_os import (
    acquire_exclusive_file_lock,
    release_file_lock,
)

META_FILENAME = "_meta.json"
PROFILE_NAME = "Unity Gateway"
MANIFEST_FILENAME = "claude-desktop.json"
MANIFEST_VERSION = 1

# Required profile fields Unity Gateway writes. Keeping the allow-list explicit preserves future
# Claude Desktop fields and user-owned values through a round trip.
OWNED_PROFILE_KEYS = (
    "inferenceProvider",
    "inferenceGatewayBaseUrl",
    "inferenceGatewayAuthScheme",
    "inferenceCredentialKind",
    "inferenceCredentialHelper",
    "inferenceCredentialHelperArgs",
    "inferenceCredentialHelperTtlSec",
    "inferenceCredentialHelperTimeoutSec",
    "inferenceCredentialHelperSilentRefreshEnabled",
    "modelDiscoveryEnabled",
    "inferenceModels",
)
OPTIONAL_PROFILE_KEYS = (
    "inferenceCustomHeaders",
    "alwaysStartWithDefaultModel",
)
ALL_PROFILE_KEYS = OWNED_PROFILE_KEYS + OPTIONAL_PROFILE_KEYS

_MISSING = object()


class ClaudeDesktopError(RuntimeError):
    """Base error for an unavailable or unsafe Claude Desktop update."""


class ClaudeDesktopUnsupportedError(ClaudeDesktopError):
    """The host has no validated native Claude Desktop profile path."""


class ClaudeDesktopUnavailableError(ClaudeDesktopError):
    """Claude Desktop has not initialized its profile directory or metadata."""


class ClaudeDesktopConflictError(ClaudeDesktopError):
    """A user or another writer changed data Unity Gateway owns."""


@dataclass(frozen=True)
class ConfigureResult:
    """Result of applying the Unity Gateway profile."""

    profile_id: str
    profile_path: Path
    metadata_path: Path
    changed: bool
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class RevertResult:
    """Result of reverting the recorded Unity Gateway profile."""

    profile_id: str | None
    changed: bool
    drifted: bool = False
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Paths:
    directory: Path
    metadata: Path
    state: Path
    lock: Path
    metadata_was_absent: bool
    directory_was_absent: bool


def claude_desktop_root_directory(
    *, home: Path | str | None = None, platform: str | None = None
) -> Path | None:
    """Return the native Claude-3p root, or ``None`` on unsupported hosts.

    Windows has no validated equivalent in this adapter yet.  Callers that pass ``directory=``
    to :func:`configure_claude_desktop` or :func:`revert_claude_desktop` are explicitly selecting
    a filesystem fixture and therefore do not use this native resolver.
    """

    platform_name = (platform if platform is not None else sys.platform).lower()
    if platform_name != "darwin":
        return None
    home_path = Path(home) if home is not None else Path.home()
    return home_path / "Library" / "Application Support" / "Claude-3p"


def claude_desktop_directory(
    *, home: Path | str | None = None, platform: str | None = None
) -> Path | None:
    """Return the validated native Claude-3p profile-storage directory.

    Claude Desktop stores the native ``_meta.json`` and UUID profiles under ``configLibrary`` on
    macOS. Windows has no validated equivalent in this adapter yet.
    """

    root = claude_desktop_root_directory(home=home, platform=platform)
    return root / "configLibrary" if root is not None else None


def render_profile(
    *,
    model_ids: Sequence[str],
    base_url: str,
    helper_command: str | Path,
    helper_args: Sequence[str],
    custom_headers: Mapping[str, str] | None = None,
    always_start_with_default_model: bool | None = None,
) -> dict[str, object]:
    """Render the exact Unity Gateway-owned Claude Desktop profile fields.

    ``model_ids`` must already be resolved full model IDs.  This adapter does not discover or
    rewrite IDs and does not add a default-model field.  ``helper_command`` is kept as supplied so
    an absolute executable path containing spaces remains a single native helper value.
    """

    if not isinstance(base_url, str) or not base_url:
        raise ValueError("Claude Desktop gateway base URL must be a non-empty string.")

    command = os.fspath(helper_command)
    if not command or not (os.path.isabs(command) or ntpath.isabs(command)):
        raise ValueError("Claude Desktop credential helper must be an absolute executable path.")

    normalized_model_ids = _normalize_model_ids(model_ids)
    normalized_args = _normalize_helper_args(helper_args)
    normalized_headers = _normalize_custom_headers(custom_headers)
    if always_start_with_default_model is not None and not isinstance(
        always_start_with_default_model, bool
    ):
        raise ValueError(
            "Claude Desktop alwaysStartWithDefaultModel must be a boolean when supplied."
        )
    profile: dict[str, object] = {
        "inferenceProvider": "gateway",
        "inferenceGatewayBaseUrl": base_url,
        "inferenceGatewayAuthScheme": "bearer",
        "inferenceCredentialKind": "helper-script",
        "inferenceCredentialHelper": command,
        "inferenceCredentialHelperArgs": normalized_args,
        "inferenceCredentialHelperTtlSec": 900,
        "inferenceCredentialHelperTimeoutSec": 300,
        "inferenceCredentialHelperSilentRefreshEnabled": True,
        "modelDiscoveryEnabled": False,
        "inferenceModels": [{"name": model_id} for model_id in normalized_model_ids],
    }
    if normalized_headers is not None:
        profile["inferenceCustomHeaders"] = normalized_headers
    if always_start_with_default_model is not None:
        profile["alwaysStartWithDefaultModel"] = always_start_with_default_model
    return profile


def configure_claude_desktop(
    workspace: str,
    *,
    model_ids: Sequence[str],
    base_url: str,
    helper_command: str | Path,
    helper_args: Sequence[str],
    custom_headers: Mapping[str, str] | None = None,
    always_start_with_default_model: bool | None = None,
    directory: Path | str | None = None,
    state_path: Path | str | None = None,
    profile_key: str | None = None,
) -> ConfigureResult:
    """Create or update the dedicated Unity Gateway profile and make it applied.

    ``directory`` may point at the exact profile-storage directory (for example an injected
    ``configLibrary`` path). If it points at the Claude-3p root, an existing ``configLibrary``
    metadata file is selected instead of creating a second unused config. Repeated calls reuse the
    profile ID recorded in the UG manifest. A pre-existing entry named ``Unity Gateway`` is never
    adopted by name alone.
    """

    if not isinstance(workspace, str) or not workspace:
        raise ValueError("Claude Desktop workspace must be a non-empty string.")
    rendered = render_profile(
        model_ids=model_ids,
        base_url=base_url,
        helper_command=helper_command,
        helper_args=helper_args,
        custom_headers=custom_headers,
        always_start_with_default_model=always_start_with_default_model,
    )
    paths = _resolve_paths(directory=directory, state_path=state_path)
    scope = _scope_key(paths.directory, workspace, profile_key)
    with _locked(paths.lock):
        metadata_original = _read_optional_object(paths.metadata, "Claude Desktop metadata")
        if metadata_original is None:
            metadata = _new_metadata()
            _validate_metadata(metadata, paths.metadata)
        else:
            metadata = metadata_original
            _validate_metadata(metadata, paths.metadata)
        state_was_absent = not paths.state.exists()
        manifest = _read_manifest(paths.state)
        profiles = manifest["profiles"]
        record_value = profiles.get(scope)
        record = _validate_record(record_value, scope) if record_value is not None else None

        if record is None:
            profile_id = _new_profile_id(metadata, paths.directory)
            profile_path = _profile_path(paths.directory, profile_id)
            profile_before: dict[str, object] | None = None
            current_profile: dict[str, object] = {}
            prior_applied_present = "appliedId" in metadata
            prior_applied = metadata.get("appliedId") if prior_applied_present else None
            if prior_applied is not None and not isinstance(prior_applied, str):
                # `_validate_metadata` normally catches this. Keep this guard close to the
                # snapshot because the value is persisted in the ownership record.
                raise ClaudeDesktopConflictError(
                    f"Claude Desktop metadata has an invalid appliedId in {paths.metadata}."
                )
            entry_after = {"id": profile_id, "name": PROFILE_NAME}
            owned_keys = _owned_keys_for_rendered(rendered)
            profile_after = _merge_owned_profile(current_profile, rendered)
            record = _new_record(
                directory=paths.directory,
                workspace=workspace,
                profile_key=profile_key,
                profile_id=profile_id,
                profile_before=profile_before,
                profile_after=profile_after,
                entry_after=entry_after,
                owned_keys=owned_keys,
                prior_applied_present=prior_applied_present,
                prior_applied=prior_applied,
            )
            record["metadata_was_absent"] = metadata_original is None
            metadata_after = _apply_metadata(metadata, profile_id, entry_after)
            profile_original = None
        else:
            profile_id = record["profile_id"]
            profile_path = _profile_path(paths.directory, profile_id)
            profile_original = _read_required_object(profile_path, "Unity Gateway profile")
            _check_profile_drift(profile_original, record, profile_path)
            _check_metadata_ownership(metadata, record, paths.metadata)
            profile_after = _merge_owned_profile(profile_original, rendered)
            metadata_after = _apply_metadata(metadata, profile_id, record["entry_after"])
            record = dict(record)
            owned_keys = _owned_keys_for_rendered(rendered, previous=record["owned_keys"])
            record["owned_keys"] = owned_keys
            record["owned_after"] = _owned_values(profile_after, owned_keys)
            # Preserve the original pre-UG snapshot. It is the baseline revert needs even when a
            # later configure refreshes model IDs or the gateway URL.
            record["profile_after"] = copy.deepcopy(profile_after)

        manifest_after = copy.deepcopy(manifest)
        manifest_after["profiles"][scope] = record

        profile_changed = profile_original != profile_after
        metadata_changed = metadata != metadata_after
        manifest_changed = manifest != manifest_after
        if not (profile_changed or metadata_changed or manifest_changed):
            return ConfigureResult(
                profile_id=profile_id,
                profile_path=profile_path,
                metadata_path=paths.metadata,
                changed=False,
                warnings=(DEFAULT_MODEL_WARNING,),
            )

        _transactional_write(
            profile_path=profile_path,
            profile_before=profile_original,
            profile_after=profile_after,
            metadata_path=paths.metadata,
            metadata_before=metadata_original,
            metadata_after=metadata_after,
            state_path=paths.state,
            state_before=None if state_was_absent else manifest,
            state_after=manifest_after,
        )
        return ConfigureResult(
            profile_id=profile_id,
            profile_path=profile_path,
            metadata_path=paths.metadata,
            changed=True,
            warnings=(DEFAULT_MODEL_WARNING,),
        )


def revert_claude_desktop(
    workspace: str,
    *,
    directory: Path | str | None = None,
    state_path: Path | str | None = None,
    profile_key: str | None = None,
) -> RevertResult:
    """Revert only the recorded Unity Gateway profile fields.

    User edits made after configure win.  In particular, ``appliedId`` is restored only while it
    still points at the Unity Gateway profile; a user's later profile selection is preserved.
    """

    if not isinstance(workspace, str) or not workspace:
        raise ValueError("Claude Desktop workspace must be a non-empty string.")
    paths = _resolve_paths(directory=directory, state_path=state_path)
    scope = _scope_key(paths.directory, workspace, profile_key)
    with _locked(paths.lock):
        manifest = _read_manifest(paths.state)
        profiles = manifest["profiles"]
        record_value = profiles.get(scope)
        if record_value is None:
            return RevertResult(profile_id=None, changed=False)
        record = _validate_record(record_value, scope)
        metadata = _read_required_object(paths.metadata, "Claude Desktop metadata")
        _validate_metadata(metadata, paths.metadata)
        _check_metadata_ownership(
            metadata,
            record,
            paths.metadata,
            allow_missing=True,
            allow_drift=True,
        )

        profile_id = record["profile_id"]
        profile_path = _profile_path(paths.directory, profile_id)
        profile_original = _read_optional_object(profile_path, "Unity Gateway profile")
        profile_after, profile_changed, profile_drifted = _revert_profile(
            profile_original,
            record,
            profile_path,
        )
        entries = metadata["entries"]
        assert isinstance(entries, list)
        entry = next((item for item in entries if item.get("id") == profile_id), None)
        if entry is not None and entry != record["entry_after"]:
            profile_after = profile_original
            profile_changed = False
            profile_drifted = True
        metadata_after, metadata_changed = _revert_metadata(
            metadata, record, directory=paths.directory, keep_entry=profile_after is not None
        )
        if record.get("metadata_was_absent") and metadata_after == {"entries": []}:
            metadata_after = None
            metadata_changed = True

        manifest_after = copy.deepcopy(manifest)
        manifest_after["profiles"].pop(scope, None)
        manifest_changed = manifest_after != manifest
        if not (profile_changed or metadata_changed or manifest_changed):
            return RevertResult(
                profile_id=profile_id,
                changed=False,
                drifted=profile_drifted,
            )

        _transactional_write(
            profile_path=profile_path,
            profile_before=profile_original,
            profile_after=profile_after,
            metadata_path=paths.metadata,
            metadata_before=metadata,
            metadata_after=metadata_after,
            state_path=paths.state,
            state_before=manifest,
            state_after=manifest_after,
        )
        return RevertResult(
            profile_id=profile_id,
            changed=True,
            drifted=profile_drifted,
        )


DEFAULT_MODEL_WARNING = (
    "Claude Desktop's native model-selection semantics are only partly validated; no model ID "
    "default field is written, and alwaysStartWithDefaultModel is written only when explicitly supplied."
)


def _normalize_model_ids(model_ids: Sequence[str]) -> list[str]:
    if isinstance(model_ids, (str, bytes)):
        raise ValueError("Claude Desktop model IDs must be a sequence of full model ID strings.")
    normalized: list[str] = []
    for model_id in model_ids:
        if not isinstance(model_id, str) or not model_id:
            raise ValueError("Claude Desktop model IDs must be non-empty strings.")
        if model_id not in normalized:
            normalized.append(model_id)
    return normalized


def _normalize_helper_args(helper_args: Sequence[str]) -> list[str]:
    if isinstance(helper_args, (str, bytes)):
        raise ValueError("Claude Desktop credential helper args must be a sequence of strings.")
    normalized = list(helper_args)
    if any(not isinstance(value, str) for value in normalized):
        raise ValueError("Claude Desktop credential helper args must contain only strings.")
    return normalized


def _normalize_custom_headers(
    custom_headers: Mapping[str, str] | None,
) -> dict[str, str] | None:
    if custom_headers is None:
        return None
    if not isinstance(custom_headers, Mapping):
        raise ValueError("Claude Desktop custom headers must be a mapping of strings.")
    normalized: dict[str, str] = {}
    for name, value in custom_headers.items():
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(value, str)
            or "\r" in name
            or "\n" in name
            or "\r" in value
            or "\n" in value
        ):
            raise ValueError(
                "Claude Desktop custom header names and values must be strings without CR/LF."
            )
        normalized[name] = value
    return normalized


def _resolve_paths(
    *,
    directory: Path | str | None,
    state_path: Path | str | None,
) -> _Paths:
    directory_was_absent = False
    if directory is None:
        native_directory = claude_desktop_directory()
        if native_directory is None:
            raise ClaudeDesktopUnsupportedError(
                "Claude Desktop profile configuration is currently supported only on macOS; "
                "Windows support is not validated yet."
            )
        resolved_directory = native_directory
        if not resolved_directory.exists():
            if not _claude_desktop_installed():
                raise ClaudeDesktopUnavailableError(
                    f"Claude Desktop profile directory is unavailable at {resolved_directory}, and "
                    "Claude Desktop was not found in the standard macOS Applications folders. "
                    "Install/open Claude Desktop, then run configure again; Unity Gateway will not "
                    "install it."
                )
            directory_was_absent = True
            try:
                resolved_directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ClaudeDesktopUnavailableError(
                    f"Could not initialize Claude Desktop profile directory at {resolved_directory}."
                ) from exc
    else:
        # An injected path is a test/embedding seam. It intentionally bypasses native platform
        # detection, but we never present it as a supported Windows or Linux app location.
        resolved_directory = Path(directory)
        if not resolved_directory.exists():
            directory_was_absent = True
            try:
                resolved_directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ClaudeDesktopUnavailableError(
                    f"Could not initialize injected Claude Desktop profile directory at {resolved_directory}."
                ) from exc
    if not resolved_directory.is_dir():
        raise ClaudeDesktopUnavailableError(
            f"Claude Desktop profile path is not a directory: {resolved_directory}."
        )
    resolved_directory = _select_profile_directory(resolved_directory)
    resolved_state = Path(state_path) if state_path is not None else APP_DIR / MANIFEST_FILENAME
    return _Paths(
        directory=resolved_directory,
        metadata=resolved_directory / META_FILENAME,
        state=resolved_state,
        lock=resolved_state.with_name(f".{resolved_state.name}.lock"),
        metadata_was_absent=not (resolved_directory / META_FILENAME).exists(),
        directory_was_absent=directory_was_absent,
    )


def _select_profile_directory(root: Path) -> Path:
    """Select the actual Claude-3p profile store without silently shadowing it.

    Public samples have used both ``Claude-3p/_meta.json`` and
    ``Claude-3p/configLibrary/_meta.json``. When one exists, use it. When both exist, forcing the
    caller to pass the exact directory avoids writing a second profile store that Claude Desktop
    will ignore. A fresh injected/native root uses the root layout until a confirmed subdirectory
    appears.
    """

    root_metadata = root / META_FILENAME
    library = root / "configLibrary"
    library_metadata = library / META_FILENAME
    if root_metadata.is_file() and library_metadata.is_file():
        raise ClaudeDesktopConflictError(
            f"Claude Desktop has profile metadata in both {root} and {library}; pass the exact "
            "profile directory explicitly instead of choosing one implicitly."
        )
    if library_metadata.is_file() or (library.is_dir() and not root_metadata.exists()):
        return library
    return root


def _claude_desktop_installed(*, home: Path | str | None = None) -> bool:
    """Best-effort macOS app presence check used before creating a native profile directory."""

    home_path = Path(home) if home is not None else Path.home()
    return any(
        path.is_dir()
        for path in (
            Path("/Applications/Claude.app"),
            home_path / "Applications" / "Claude.app",
        )
    )


@contextmanager
def _locked(lock_path: Path) -> Iterator[None]:
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as handle:
            acquire_exclusive_file_lock(handle)
            try:
                yield
            finally:
                release_file_lock(handle)
    except OSError as exc:
        raise ClaudeDesktopError(
            f"Could not lock Claude Desktop profile state at {lock_path}."
        ) from exc


def _read_required_object(path: Path, label: str) -> dict[str, object]:
    value = _read_optional_object(path, label)
    if value is None:
        if path.name == META_FILENAME:
            raise ClaudeDesktopUnavailableError(
                f"Claude Desktop metadata is missing at {path}. Open Claude Desktop once, then "
                "run configure again; Unity Gateway will not create app metadata."
            )
        raise ClaudeDesktopConflictError(
            f"{label} is missing at {path}; refusing to recreate it unsafely."
        )
    return value


def _read_optional_object(path: Path, label: str) -> dict[str, object] | None:
    try:
        if not path.exists():
            return None
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw)
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ClaudeDesktopConflictError(
            f"{label} at {path} is malformed; refusing to overwrite it."
        ) from exc
    if not isinstance(value, dict):
        raise ClaudeDesktopConflictError(f"{label} at {path} must contain a JSON object.")
    return value


def _validate_metadata(metadata: Mapping[str, object], path: Path) -> None:
    entries = metadata.get("entries")
    if not isinstance(entries, list):
        raise ClaudeDesktopConflictError(
            f"Claude Desktop metadata at {path} has no valid entries list."
        )
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ClaudeDesktopConflictError(
                f"Claude Desktop metadata at {path} contains an invalid profile entry."
            )
        profile_id = entry.get("id")
        if not isinstance(profile_id, str) or not _is_uuid(profile_id):
            raise ClaudeDesktopConflictError(
                f"Claude Desktop metadata at {path} contains an invalid profile ID."
            )
        if profile_id in seen:
            raise ClaudeDesktopConflictError(
                f"Claude Desktop metadata at {path} contains duplicate profile ID {profile_id}."
            )
        seen.add(profile_id)
        if not isinstance(entry.get("name"), str):
            raise ClaudeDesktopConflictError(
                f"Claude Desktop metadata at {path} contains an invalid profile name."
            )
    applied_id = metadata.get("appliedId", _MISSING)
    if applied_id is not _MISSING and (not isinstance(applied_id, str) or not _is_uuid(applied_id)):
        raise ClaudeDesktopConflictError(
            f"Claude Desktop metadata at {path} contains an invalid appliedId."
        )


def _validate_record(value: object, scope: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ClaudeDesktopConflictError(
            f"Unity Gateway ownership record for {scope} is malformed."
        )
    profile_id = value.get("profile_id")
    if not isinstance(profile_id, str) or not _is_uuid(profile_id):
        raise ClaudeDesktopConflictError(
            f"Unity Gateway ownership record for {scope} has an invalid profile ID."
        )
    entry_after = value.get("entry_after")
    if not isinstance(entry_after, dict) or entry_after != {
        "id": profile_id,
        "name": PROFILE_NAME,
    }:
        raise ClaudeDesktopConflictError(
            f"Unity Gateway ownership record for {scope} has an invalid metadata entry."
        )
    profile_before = value.get("profile_before")
    if profile_before is not None and not isinstance(profile_before, dict):
        raise ClaudeDesktopConflictError(
            f"Unity Gateway ownership record for {scope} has an invalid profile baseline."
        )
    owned_after = value.get("owned_after")
    if not isinstance(owned_after, dict):
        profile_after = value.get("profile_after")
        if not isinstance(profile_after, dict):
            raise ClaudeDesktopConflictError(
                f"Unity Gateway ownership record for {scope} is missing owned values."
            )
        owned_after = _owned_values(profile_after)
    owned_keys_value = value.get("owned_keys")
    if owned_keys_value is None:
        owned_keys = list(owned_after)
    elif isinstance(owned_keys_value, list) and all(
        isinstance(key, str) for key in owned_keys_value
    ):
        owned_keys = list(owned_keys_value)
    else:
        raise ClaudeDesktopConflictError(
            f"Unity Gateway ownership record for {scope} has invalid owned keys."
        )
    if not set(OWNED_PROFILE_KEYS).issubset(owned_keys) or not set(owned_keys).issubset(
        ALL_PROFILE_KEYS
    ):
        raise ClaudeDesktopConflictError(
            f"Unity Gateway ownership record for {scope} has incomplete owned keys."
        )
    if set(owned_after) != set(owned_keys):
        raise ClaudeDesktopConflictError(
            f"Unity Gateway ownership record for {scope} has incomplete owned values."
        )
    prior_applied_present = value.get("prior_applied_present")
    prior_applied = value.get("prior_applied")
    if not isinstance(prior_applied_present, bool):
        raise ClaudeDesktopConflictError(
            f"Unity Gateway ownership record for {scope} is missing appliedId state."
        )
    if prior_applied_present and (
        not isinstance(prior_applied, str) or not _is_uuid(prior_applied)
    ):
        raise ClaudeDesktopConflictError(
            f"Unity Gateway ownership record for {scope} has an invalid appliedId baseline."
        )
    normalized = dict(value)
    normalized["owned_after"] = owned_after
    normalized["owned_keys"] = owned_keys
    normalized["profile_before"] = profile_before
    normalized["entry_after"] = dict(entry_after)
    return normalized


def _new_record(
    *,
    directory: Path,
    workspace: str,
    profile_key: str | None,
    profile_id: str,
    profile_before: dict[str, object] | None,
    profile_after: dict[str, object],
    entry_after: Mapping[str, object],
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
        "owned_after": _owned_values(profile_after, owned_keys),
        "entry_after": copy.deepcopy(entry_after),
        "prior_applied_present": prior_applied_present,
        "prior_applied": prior_applied,
    }


def _read_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": MANIFEST_VERSION, "profiles": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ClaudeDesktopConflictError(
            f"Unity Gateway Claude Desktop ownership state at {path} is malformed; refusing to overwrite it."
        ) from exc
    if not isinstance(value, dict) or value.get("version") != MANIFEST_VERSION:
        raise ClaudeDesktopConflictError(
            f"Unity Gateway Claude Desktop ownership state at {path} has an unsupported version."
        )
    profiles = value.get("profiles")
    if not isinstance(profiles, dict):
        raise ClaudeDesktopConflictError(
            f"Unity Gateway Claude Desktop ownership state at {path} is malformed."
        )
    return value


def _new_metadata() -> dict[str, object]:
    return {"entries": []}


def _scope_key(directory: Path, workspace: str, profile_key: str | None) -> str:
    # JSON encoding avoids collisions from separators in workspace URLs or injected paths.
    return json.dumps(
        [str(directory), workspace, profile_key or "default"],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError):
        return False
    return True


def _new_profile_id(metadata: Mapping[str, object], directory: Path) -> str:
    entries = metadata.get("entries")
    if not isinstance(entries, list):
        raise ClaudeDesktopConflictError("Claude Desktop metadata has no valid entries list.")
    existing = {
        entry.get("id")
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    }
    while True:
        profile_id = str(uuid.uuid4())
        if profile_id not in existing and not _profile_path(directory, profile_id).exists():
            return profile_id


def _profile_path(directory: Path, profile_id: str) -> Path:
    # IDs are validated before reaching this helper; this guard also protects future callers.
    if not _is_uuid(profile_id):
        raise ClaudeDesktopConflictError(f"Invalid Claude Desktop profile ID: {profile_id!r}.")
    return directory / f"{profile_id}.json"


def _merge_owned_profile(
    profile: Mapping[str, object], rendered: Mapping[str, object]
) -> dict[str, object]:
    merged = copy.deepcopy(dict(profile))
    for key in OWNED_PROFILE_KEYS:
        merged[key] = copy.deepcopy(rendered[key])
    for key in OPTIONAL_PROFILE_KEYS:
        if key in rendered:
            merged[key] = copy.deepcopy(rendered[key])
    return merged


def _owned_keys_for_rendered(
    rendered: Mapping[str, object], *, previous: Sequence[str] | None = None
) -> list[str]:
    keys = list(previous) if previous is not None else list(OWNED_PROFILE_KEYS)
    for key in OPTIONAL_PROFILE_KEYS:
        if key in rendered and key not in keys:
            keys.append(key)
    return keys


def _owned_values(
    profile: Mapping[str, object], keys: Sequence[str] = OWNED_PROFILE_KEYS
) -> dict[str, object]:
    return {key: copy.deepcopy(profile[key]) for key in keys if key in profile}


def _check_profile_drift(
    profile: Mapping[str, object], record: Mapping[str, Any], path: Path
) -> None:
    expected = record.get("owned_after")
    if not isinstance(expected, dict):
        raise ClaudeDesktopConflictError(
            f"Unity Gateway ownership record for {path} is missing owned values."
        )
    conflicts = [key for key, value in expected.items() if profile.get(key, _MISSING) != value]
    if conflicts:
        joined = ", ".join(conflicts)
        raise ClaudeDesktopConflictError(
            f"Claude Desktop Unity Gateway profile {path} changed outside Unity Gateway ({joined}); "
            "review it or revert that profile before configuring again."
        )


def _check_metadata_ownership(
    metadata: Mapping[str, object],
    record: Mapping[str, Any],
    path: Path,
    *,
    allow_missing: bool = False,
    allow_drift: bool = False,
) -> None:
    profile_id = record["profile_id"]
    expected_entry = record["entry_after"]
    entries = metadata.get("entries")
    if not isinstance(entries, list):
        raise ClaudeDesktopConflictError(
            f"Claude Desktop metadata at {path} has no valid entries list."
        )
    matching = [
        entry for entry in entries if isinstance(entry, dict) and entry.get("id") == profile_id
    ]
    if not matching:
        if allow_missing:
            return
        raise ClaudeDesktopConflictError(
            f"Claude Desktop Unity Gateway profile {profile_id} is missing from {path}; refusing to recreate it."
        )
    if len(matching) != 1 or matching[0] != expected_entry:
        if allow_drift:
            return
        raise ClaudeDesktopConflictError(
            f"Claude Desktop Unity Gateway metadata entry {profile_id} changed outside Unity Gateway."
        )


def _apply_metadata(
    metadata: Mapping[str, object], profile_id: str, entry: Mapping[str, object]
) -> dict[str, object]:
    result = copy.deepcopy(dict(metadata))
    entries = result.get("entries")
    if not isinstance(entries, list):
        raise ClaudeDesktopConflictError("Claude Desktop metadata has no valid entries list.")
    if not any(isinstance(item, dict) and item.get("id") == profile_id for item in entries):
        entries.append(copy.deepcopy(dict(entry)))
    result["appliedId"] = profile_id
    return result


def _revert_profile(
    profile: dict[str, object] | None,
    record: Mapping[str, Any],
    path: Path,
) -> tuple[dict[str, object] | None, bool, bool]:
    if profile is None:
        return None, False, False
    expected = record["owned_after"]
    baseline = record["profile_before"]
    if not isinstance(expected, dict) or (baseline is not None and not isinstance(baseline, dict)):
        raise ClaudeDesktopConflictError(f"Unity Gateway ownership record for {path} is malformed.")
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


def _revert_metadata(
    metadata: Mapping[str, object], record: Mapping[str, Any], *, directory: Path, keep_entry: bool
) -> tuple[dict[str, object], bool]:
    result = copy.deepcopy(dict(metadata))
    profile_id = record["profile_id"]
    expected_entry = record["entry_after"]
    entries = result.get("entries")
    if not isinstance(entries, list):
        raise ClaudeDesktopConflictError("Claude Desktop metadata has no valid entries list.")
    result["entries"] = (
        entries
        if keep_entry
        else [
            entry
            for entry in entries
            if not (
                isinstance(entry, dict)
                and entry.get("id") == profile_id
                and entry == expected_entry
            )
        ]
    )
    if result.get("appliedId") == profile_id:
        prior = record["prior_applied"]
        prior_exists = (
            isinstance(prior, str)
            and any(item.get("id") == prior for item in result["entries"])
            and _profile_path(directory, prior).is_file()
        )
        if record["prior_applied_present"] and prior_exists:
            result["appliedId"] = record["prior_applied"]
        elif not keep_entry:
            result.pop("appliedId", None)
    return result, result != metadata


def _transactional_write(
    *,
    profile_path: Path,
    profile_before: dict[str, object] | None,
    profile_after: dict[str, object] | None,
    metadata_path: Path,
    metadata_before: dict[str, object] | None,
    metadata_after: dict[str, object] | None,
    state_path: Path,
    state_before: dict[str, object] | None,
    state_after: dict[str, object],
) -> None:
    """Write profile, metadata, and ownership state with best-effort atomic rollback."""

    for path, before in (
        (profile_path, profile_before),
        (metadata_path, metadata_before),
        (state_path, state_before),
    ):
        if _read_optional_object(path, "Claude Desktop configuration") != before:
            raise ClaudeDesktopConflictError(
                "Claude Desktop configuration changed during setup. Quit Claude Desktop and retry."
            )
    originals = {
        path: path.read_bytes() if before is not None else None
        for path, before in (
            (profile_path, profile_before),
            (metadata_path, metadata_before),
            (state_path, state_before),
        )
    }
    profile_written = False
    metadata_written = False
    state_written = False
    try:
        if profile_before != profile_after:
            profile_written = True
            _write_or_remove(profile_path, profile_after)
        if metadata_before != metadata_after:
            metadata_written = True
            _write_or_remove(metadata_path, metadata_after)
        if state_before != state_after:
            state_written = True
            atomic_write_json(state_path, state_after)
    except Exception as exc:  # noqa: BLE001 - rollback must preserve the original setup failure
        rollback_errors: list[Exception] = []
        if profile_written:
            try:
                _restore_original(profile_path, originals[profile_path])
            except Exception as rollback_exc:  # noqa: BLE001
                rollback_errors.append(rollback_exc)
        if metadata_written:
            try:
                _restore_original(metadata_path, originals[metadata_path])
            except Exception as rollback_exc:  # noqa: BLE001
                rollback_errors.append(rollback_exc)
        if state_written:
            try:
                _restore_original(state_path, originals[state_path])
            except Exception as rollback_exc:  # noqa: BLE001
                rollback_errors.append(rollback_exc)
        suffix = ""
        if rollback_errors:
            suffix = f" Rollback also failed: {rollback_errors[0]}"
        raise ClaudeDesktopError(
            f"Claude Desktop configuration failed and was rolled back: {exc}.{suffix}"
        ) from exc


def _write_or_remove(path: Path, payload: dict[str, object] | None) -> None:
    if payload is None:
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ClaudeDesktopError(f"Could not remove Claude Desktop profile {path}.") from exc
        return
    atomic_write_json(path, payload)


def _restore_original(path: Path, contents: bytes | None) -> None:
    if contents is None:
        _write_or_remove(path, None)
        return
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(contents)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
