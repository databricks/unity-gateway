"""Agent-independent JSON settings storage scoped to a single launch.

Storage changes alone do not reload an agent. Agent adapters choose a settings source
and determine which settings their native client can refresh while running.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ucode.config_io import atomic_write_json, deep_merge_dict, prune_key_paths
from ucode.os_compatibility.file_lock_cross_os import acquire_exclusive_file_lock, release_file_lock


def _validated_copy(settings: object) -> dict:
    def check(value: object) -> None:
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise ValueError("settings keys must be strings")
            for child in value.values():
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)
        elif value is not None and not isinstance(value, (str, bool, int, float)):
            raise ValueError("settings values must be JSON values")

    try:
        if not isinstance(settings, dict):
            raise ValueError("settings must be an object")
        check(settings)
        result = json.loads(json.dumps(settings, allow_nan=False))
        if "env" in result and (
            not isinstance(result["env"], dict)
            or any(not isinstance(value, str) for value in result["env"].values())
        ):
            raise ValueError("environment values must be strings")
    except (ValueError, TypeError, RecursionError) as exc:
        raise RuntimeError(
            "Session settings must be a JSON object with string environment values."
        ) from exc
    return result


def create_session_settings(
    settings: dict | None = None, *, filename: str = "settings.json"
) -> Path:
    """Create a private directory and initialize one session's settings file."""
    if not filename or Path(filename).name != filename or filename in (".", ".."):
        raise ValueError("Session settings filename must be a single filename.")
    payload = _validated_copy({} if settings is None else settings)
    path = Path(tempfile.mkdtemp(prefix="ug-session-settings-")) / filename
    atomic_write_json(path, payload)
    return path


def read_session_settings(path: Path) -> dict:
    """Read a complete settings snapshot; reject missing or malformed state."""
    try:
        return _validated_copy(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
        raise RuntimeError(f"Session settings at {path} are missing or invalid.") from exc


@contextmanager
def _writer_lock(path: Path) -> Iterator[None]:
    # Lock a stable sidecar, since atomic replacement changes the settings inode.
    with path.with_name(path.name + ".lock").open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        acquire_exclusive_file_lock(handle)
        try:
            yield
        finally:
            release_file_lock(handle)


def update_session_settings(
    path: Path, updates: dict, *, remove_paths: list[list[str]] | None = None
) -> dict:
    """Merge updates and explicit removals in one locked, atomic read-modify-write.

    Nested objects merge; scalar and list values replace. Removals happen first,
    so an update to the same key wins. Return the complete committed snapshot.
    """
    overlay = _validated_copy(updates)
    removals = [] if remove_paths is None else remove_paths
    if any(not keys or any(not isinstance(key, str) for key in keys) for keys in removals):
        raise ValueError("Session setting removals must contain nonempty string key paths.")
    with _writer_lock(path):
        current = read_session_settings(path)
        prune_key_paths(current, removals)
        merged = _validated_copy(deep_merge_dict(current, overlay))
        atomic_write_json(path, merged)
        return merged


def replace_session_settings(path: Path, settings: dict) -> None:
    """Explicitly replace an existing session snapshot; use update to preserve siblings."""
    payload = _validated_copy(settings)
    with _writer_lock(path):
        read_session_settings(path)
        atomic_write_json(path, payload)
