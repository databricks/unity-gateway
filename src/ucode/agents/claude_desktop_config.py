"""Claude Desktop profile rendering and file persistence."""

from __future__ import annotations

import json
import ntpath
import os
import tempfile
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path

from ucode.config_io import atomic_write_json
from ucode.os_compatibility.file_lock_cross_os import (
    acquire_exclusive_file_lock,
    release_file_lock,
)

META_FILENAME = "_meta.json"
PROFILE_NAME = "Unity Gateway"

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


@contextmanager
def locked(lock_path: Path) -> Iterator[None]:
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


def read_required_object(path: Path, label: str) -> dict[str, object]:
    value = read_optional_object(path, label)
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


def read_optional_object(path: Path, label: str) -> dict[str, object] | None:
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


def validate_metadata(metadata: Mapping[str, object], path: Path) -> None:
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
        if not isinstance(profile_id, str) or not is_uuid(profile_id):
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
    if applied_id is not _MISSING and (not isinstance(applied_id, str) or not is_uuid(applied_id)):
        raise ClaudeDesktopConflictError(
            f"Claude Desktop metadata at {path} contains an invalid appliedId."
        )


def new_metadata() -> dict[str, object]:
    return {"entries": []}


def is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError):
        return False
    return True


def profile_path(directory: Path, profile_id: str) -> Path:
    # IDs are validated before reaching this helper; this guard also protects future callers.
    if not is_uuid(profile_id):
        raise ClaudeDesktopConflictError(f"Invalid Claude Desktop profile ID: {profile_id!r}.")
    return directory / f"{profile_id}.json"


def write_configuration(
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
    retired_profiles: Mapping[Path, dict[str, object] | None] | None = None,
) -> None:
    """Write profile, metadata, and ownership state with best-effort atomic rollback."""

    changes = [
        (profile_path, profile_before, profile_after),
        *((path, before, None) for path, before in (retired_profiles or {}).items()),
        (metadata_path, metadata_before, metadata_after),
        (state_path, state_before, state_after),
    ]
    for path, before, _after in changes:
        if read_optional_object(path, "Claude Desktop configuration") != before:
            raise ClaudeDesktopConflictError(
                "Claude Desktop configuration changed during setup. Quit Claude Desktop and retry."
            )
    originals = {
        path: path.read_bytes() if before is not None else None for path, before, _after in changes
    }
    written: list[Path] = []
    try:
        for path, before, after in changes:
            if before != after:
                written.append(path)
                _write_or_remove(path, after)
    except Exception as exc:  # noqa: BLE001 - rollback must preserve the original setup failure
        rollback_errors: list[Exception] = []
        for path in reversed(written):
            try:
                _restore_original(path, originals[path])
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
