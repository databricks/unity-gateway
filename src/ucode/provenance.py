"""Per-file record of the settings key paths ug wrote, and the exact values it wrote there.

Later reconciles use the record to tell ug's own keys apart from a user's, so ug only ever
restores or deletes values it can prove it wrote. ``ug revert`` clears the store.
"""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ucode import managed_files
from ucode.config_io import is_dry_run
from ucode.os_compatibility.file_lock_cross_os import (
    acquire_exclusive_file_lock,
    release_file_lock,
)

KeyPath = tuple[str, ...]


def load_provenance(agent: str, path: Path) -> dict[KeyPath, Any] | None:
    """Return the key paths ug recorded for this file, or None when there is no record yet."""
    store = _load_store()
    if store is None:
        return None
    return _parse_entries(store["files"].get(_file_key(agent, path)))


def save_provenance(agent: str, path: Path, owned: dict[KeyPath, Any]) -> None:
    """Replace the recorded key paths and values ug wrote to this file."""
    if is_dry_run():
        return
    with _store_lock():
        store = _load_store() or {"files": {}}
        store["files"][_file_key(agent, path)] = [
            {"path": list(key_path), "value": value} for key_path, value in sorted(owned.items())
        ]
        managed_files._write_private_file(_store_path(), json.dumps(store, indent=2) + "\n")


def clear_provenance() -> None:
    """Delete the whole store, as part of ``ug revert``; a missing store is fine."""
    if is_dry_run() or not _store_path().exists():
        return
    try:
        with _store_lock():
            _store_path().unlink(missing_ok=True)
    except OSError as exc:
        raise RuntimeError(f"Could not remove the provenance store: {exc}") from exc


def owned_after_write(
    owned: dict[KeyPath, Any],
    generated: dict[KeyPath, Any],
    final: dict,
    before: dict | None = None,
) -> dict[KeyPath, Any]:
    """Keep generated paths that landed in ``final`` and prior owned paths still intact there.

    With ``before`` (the file before this write), a generated value the file already held is
    adopted only if that held value was still ug's own, so an equal value someone else wrote, or an
    edit that happens to match, stays theirs.
    """
    result: dict[KeyPath, Any] = {
        key_path: copy.deepcopy(value)
        for key_path, value in generated.items()
        if _is_intact(final, key_path, value)
        and (
            before is None
            or not _is_intact(before, key_path, value)
            or (key_path in owned and _is_intact(before, key_path, owned[key_path]))
        )
    }
    for key_path, value in owned.items():
        if key_path not in result and _is_intact(final, key_path, value):
            result[key_path] = copy.deepcopy(value)
    return result


def values_at(key_paths: list[list[str]], doc: dict | None) -> dict[KeyPath, Any]:
    """The values ``doc`` holds at ``key_paths``, skipping paths it lacks; None means no document."""
    if doc is None:
        return {}
    owned: dict[KeyPath, Any] = {}
    for key_path in map(tuple, key_paths):
        value = _value_at(doc, key_path)
        if value is not managed_files._MISSING:
            owned[key_path] = copy.deepcopy(value)
    return owned


def retire(
    doc: dict, owned: dict[KeyPath, Any], paths: Iterable[KeyPath], baseline: dict | None
) -> None:
    """Restore each owned, unedited path to its baseline value, or delete it when none exists."""
    for key_path in paths:
        if key_path in owned and _is_intact(doc, key_path, owned[key_path]):
            _restore(doc, key_path, baseline)


def retire_group(
    doc: dict, owned: dict[KeyPath, Any], paths: Iterable[KeyPath], baseline: dict | None
) -> None:
    """Like :func:`retire`, but all or nothing.

    A member that ug owns but was edited or deleted, or a present member that ug does not own and
    that differs from the baseline, vetoes the group; the veto drops ug's ownership of every member,
    so a later run never retires a remnant of someone else's group. Unowned members still at their
    baseline value stay.
    """
    paths = list(paths)
    present = [
        key_path for key_path in paths if _value_at(doc, key_path) is not managed_files._MISSING
    ]

    def vetoes(key_path: KeyPath) -> bool:
        if key_path in owned:
            return not _is_intact(doc, key_path, owned[key_path])
        return key_path in present and (
            baseline is None or not _is_intact(baseline, key_path, _value_at(doc, key_path))
        )

    if any(vetoes(key_path) for key_path in paths):
        for key_path in paths:
            owned.pop(key_path, None)
        return
    for key_path in present:
        _restore(doc, key_path, baseline)


def _store_path() -> Path:
    return managed_files.MANAGED_BACKUP_DIR / "provenance.json"


@contextmanager
def _store_lock() -> Iterator[None]:
    """Serialize the store's read-modify-write across concurrent ug processes with an OS file lock.

    Keep the lock file in place: unlinking it could let waiters lock different inodes.
    """
    backup_dir = managed_files.MANAGED_BACKUP_DIR
    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    with (backup_dir / "provenance.lock").open("a+b") as lock_file:
        acquire_exclusive_file_lock(lock_file)
        try:
            yield
        finally:
            release_file_lock(lock_file)


def _file_key(agent: str, path: Path) -> str:
    return f"{agent}:{path}"


def _load_store() -> dict | None:
    try:
        store = json.loads(_store_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(store, dict) or not isinstance(store.get("files"), dict):
        return None
    return store


def _parse_entries(entries: object) -> dict[KeyPath, Any] | None:
    if not isinstance(entries, list):
        return None
    owned: dict[KeyPath, Any] = {}
    for entry in entries:
        if not isinstance(entry, dict) or "value" not in entry:
            continue
        path = entry.get("path")
        if isinstance(path, list) and all(isinstance(part, str) for part in path):
            owned[tuple(path)] = entry["value"]
    return owned


def _value_at(doc: dict, key_path: KeyPath) -> Any:
    return managed_files._path_value(doc, list(key_path))


def _is_intact(doc: dict, key_path: KeyPath, value: Any) -> bool:
    """Whether ``doc`` still holds exactly ``value`` at ``key_path``, comparing types strictly."""
    current = _value_at(doc, key_path)
    if current is managed_files._MISSING:
        return False
    return managed_files.is_semantically_equal(current, value)


def _restore(doc: dict, key_path: KeyPath, baseline: dict | None) -> None:
    value = _value_at(baseline, key_path) if baseline is not None else managed_files._MISSING
    if value is managed_files._MISSING:
        managed_files._delete_path_value(doc, list(key_path))
    else:
        managed_files._set_path_value(doc, list(key_path), value)
