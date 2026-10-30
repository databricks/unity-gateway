"""Transactional installation of UG's system-wide Desktop plugin."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path

from ucode import config_io, managed_files
from ucode.os_compatibility import system_cross_os

Bundle = dict[str, tuple[bytes, int]]
_DIGEST_KEYS = ("system_digest", "pending_system_digest")


def read_bundle(root: Path) -> Bundle | None:
    for path in (root, *root.parents):
        if path.is_symlink() or path.is_junction():
            raise RuntimeError(f"Refusing linked Desktop plugin path: {path}")
    if not root.exists():
        return None
    if not root.is_dir():
        raise RuntimeError(f"Desktop plugin path is not a directory: {root}")
    files = {}
    for path in sorted(root.rglob("*")):
        mode = path.lstat().st_mode
        if (
            path.is_symlink()
            or path.is_junction()
            or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))
        ):
            raise RuntimeError(f"Refusing non-regular Desktop plugin content: {path}")
        if path.is_file():
            files[path.relative_to(root).as_posix()] = (
                path.read_bytes(),
                0o755 if mode & 0o111 else 0o644,
            )
    return files


def bundle_digest(files: Bundle) -> str:
    digest = hashlib.sha256()
    for name, (content, mode) in sorted(files.items()):
        digest.update(json.dumps([name, mode, len(content)]).encode("utf-8"))
        digest.update(content)
    return digest.hexdigest()


def _write_bundle(root: Path, files: Bundle) -> None:
    root.mkdir()
    for name, (content, mode) in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(mode)


def _owned_bundle(path: Path, state: dict) -> Bundle | None:
    files = read_bundle(path)
    if files is not None and bundle_digest(files) not in [state.get(key) for key in _DIGEST_KEYS]:
        raise RuntimeError(
            f"Desktop plugin at {path} is not this user's UG installation or was edited outside "
            "UG. It was left unchanged; resolve the existing installation before retrying."
        )
    return files


def install(source: Path, state_path: Path) -> None:
    path = managed_files.claude_desktop_plugin_path()
    if path is None or config_io.is_dry_run():
        return
    state = config_io.read_json_safe(state_path)
    previous = _owned_bundle(path, state)
    files = read_bundle(source)
    if files is None:
        raise RuntimeError(f"Desktop plugin source is missing: {source}")
    manifest_name = ".claude-plugin/plugin.json"
    manifest = json.loads(files[manifest_name][0])
    manifest["installationPreference"] = "required"
    files[manifest_name] = (json.dumps(manifest, indent=2).encode("utf-8") + b"\n", 0o644)
    version = "0.0.0-ug-" + bundle_digest(files)
    manifest["version"] = version
    files[manifest_name] = (json.dumps(manifest, indent=2).encode("utf-8") + b"\n", 0o644)
    files["version.json"] = (json.dumps({"version": version}).encode("utf-8") + b"\n", 0o644)
    digest = bundle_digest(files)
    # Retain ownership if the process stops after installation but before saving its result.
    config_io.atomic_write_json(state_path, {**state, "pending_system_digest": digest})
    try:
        _update_bundle(path, files, previous)
    except Exception:
        config_io.atomic_write_json(state_path, state)
        raise
    state.update(version=2, system_digest=digest)
    state.pop("pending_system_digest", None)
    config_io.atomic_write_json(state_path, state)


def remove(state_path: Path) -> bool:
    path = managed_files.claude_desktop_plugin_path()
    state = config_io.read_json_safe(state_path)
    if path is None or not any(state.get(key) for key in _DIGEST_KEYS) or config_io.is_dry_run():
        return False
    previous = _owned_bundle(path, state)
    _update_bundle(path, None, previous)
    for key in _DIGEST_KEYS:
        state.pop(key, None)
    config_io.atomic_write_json(state_path, state)
    return previous is not None


def _update_bundle(path: Path, files: Bundle | None, previous: Bundle | None) -> None:
    if files == previous:
        return
    if path != managed_files.claude_desktop_plugin_path():
        raise RuntimeError(f"Refusing unexpected Desktop plugin target: {path}")
    if managed_files.current_os() is managed_files.OS.WINDOWS:
        if not system_cross_os.is_admin():
            raise RuntimeError(
                "Desktop plugin installation requires an elevated terminal on Windows."
            )
        _replace_windows_bundle(path, files, previous)
    else:
        with tempfile.TemporaryDirectory(prefix="ug-desktop-plugin-") as directory:
            request = Path(directory)
            if files is not None:
                _write_bundle(request / "new", files)
            if previous is not None:
                _write_bundle(request / "previous", previous)
            managed_files.replace_privileged_bundle(path, request)
    if read_bundle(path) != files:
        raise RuntimeError(f"Desktop plugin verification failed at {path}; rerun ug configure.")


def _replace_windows_bundle(path: Path, files: Bundle | None, previous: Bundle | None) -> None:
    if read_bundle(path) != previous:
        raise RuntimeError(f"Desktop plugin changed before installation: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.parent / ".ug-install.lock"
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise RuntimeError(
            f"Another Desktop plugin update holds {lock}; retry after it finishes."
        ) from exc
    staging = None
    retain_backup = False
    try:
        staging = Path(tempfile.mkdtemp(prefix=".ug-install-", dir=path.parent))
        if files is not None:
            _write_bundle(staging / "new", files)
        if read_bundle(path) != previous:
            raise RuntimeError(f"Desktop plugin changed before installation: {path}")
        if previous is not None:
            os.replace(path, staging / "previous")
        try:
            if files is not None:
                os.replace(staging / "new", path)
            if read_bundle(path) != files:
                raise RuntimeError(f"Desktop plugin verification failed at {path}")
        except Exception:
            try:
                if path.exists():
                    shutil.rmtree(path)
                if previous is not None:
                    os.replace(staging / "previous", path)
            except OSError as exc:
                retain_backup = True
                raise RuntimeError(
                    f"Desktop plugin backup retained at {staging / 'previous'}"
                ) from exc
            raise
    finally:
        if staging is not None and not retain_backup:
            shutil.rmtree(staging)
        lock.rmdir()
