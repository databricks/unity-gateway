"""Claude Desktop profile rendering and file persistence."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import NotRequired, TypedDict, cast

from ucode.config_io import atomic_write_json
from ucode.os_compatibility.file_lock_cross_os import (
    acquire_exclusive_file_lock,
    release_file_lock,
)

META_FILENAME = "_meta.json"
PROFILE_NAME = "Unity Gateway"
PROFILE_LOCK_TIMEOUT_SECONDS = 5

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
OPTIONAL_PROFILE_KEYS = ("inferenceCustomHeaders",)
ALL_PROFILE_KEYS = OWNED_PROFILE_KEYS + OPTIONAL_PROFILE_KEYS

_MISSING = object()


class ProfileEntry(TypedDict):
    id: str
    name: str


class Metadata(TypedDict):
    entries: list[ProfileEntry]
    appliedId: NotRequired[str]


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
) -> dict[str, object]:
    """Render resolved models, headers, and literal helper arguments in Desktop's schema."""

    profile: dict[str, object] = {
        "inferenceProvider": "gateway",
        "inferenceGatewayBaseUrl": base_url,
        "inferenceGatewayAuthScheme": "bearer",
        "inferenceCredentialKind": "helper-script",
        "inferenceCredentialHelper": os.fspath(helper_command),
        "inferenceCredentialHelperArgs": list(helper_args),
        "inferenceCredentialHelperTtlSec": 900,
        "inferenceCredentialHelperTimeoutSec": 300,
        "inferenceCredentialHelperSilentRefreshEnabled": True,
        "modelDiscoveryEnabled": False,
        "inferenceModels": [{"name": model_id} for model_id in model_ids],
    }
    if custom_headers is not None:
        profile["inferenceCustomHeaders"] = dict(custom_headers)
    return profile


@contextmanager
def locked(lock_path: Path) -> Iterator[None]:
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as handle:
            try:
                acquire_exclusive_file_lock(handle, timeout=PROFILE_LOCK_TIMEOUT_SECONDS)
            except TimeoutError as exc:
                raise ClaudeDesktopError(
                    "Timed out waiting for another Unity Gateway profile update. "
                    "Finish that operation and retry."
                ) from exc
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


def validate_metadata(metadata: Mapping[str, object], path: Path) -> Metadata:
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
    return cast(Metadata, metadata)


def new_metadata() -> Metadata:
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
    metadata_before: Mapping[str, object] | None,
    metadata_after: Mapping[str, object] | None,
    state_path: Path,
    state_before: dict[str, object] | None,
    state_after: dict[str, object],
) -> None:
    """Write profile, metadata, and ownership state with best-effort atomic rollback."""

    changes = [
        (profile_path, profile_before, profile_after),
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


def _write_or_remove(path: Path, payload: Mapping[str, object] | None) -> None:
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
