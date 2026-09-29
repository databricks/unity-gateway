"""Install the skills packaged with Unity Gateway."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.metadata import distribution
from pathlib import Path
from typing import BinaryIO

_SKILL_ROOTS = (".claude/skills", ".agents/skills")
_SKILL_NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
SMART_ROUTER_SKILL = "smart-router"
_LOCK_TIMEOUT_SECONDS = 10.0
_LOCK_POLL_INTERVAL_SECONDS = 0.05


def _skills_source() -> Path:
    packaged = Path(str(distribution("unity-gateway").locate_file("skills")))
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parents[2] / "skills"


def _validate_skill_name(skill_name: str) -> None:
    if _SKILL_NAME_PATTERN.fullmatch(skill_name) is None:
        raise ValueError(f"Invalid skill name: {skill_name!r}")


def _remove_skill_path(destination: Path) -> bool:
    """Remove an existing skill path so an install is an exact replacement."""
    if destination.is_symlink() or destination.is_file():
        destination.unlink()
        return True
    if destination.is_dir():
        shutil.rmtree(destination)
        return True
    return False


def _path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _lock_file(lock_file: BinaryIO, timeout_seconds: float) -> None:
    if os.name == "nt":
        import msvcrt

        lock_file.seek(0)
        if not lock_file.read(1):
            lock_file.write(b"\0")
            lock_file.flush()

    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            if os.name == "nt":
                import msvcrt

                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            pass
        except OSError:
            if os.name != "nt":
                raise

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(
                "Timed out waiting for another Unity Gateway process to finish updating "
                "packaged skills. Retry after the other agent launch completes."
            )
        time.sleep(min(_LOCK_POLL_INTERVAL_SECONDS, remaining))


def _unlock_file(lock_file: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(lock_file, fcntl.LOCK_UN)


@contextmanager
def _installation_lock(
    base: Path, *, timeout_seconds: float = _LOCK_TIMEOUT_SECONDS
) -> Iterator[None]:
    """Serialize packaged-skill changes across concurrent agent launches.

    Keep the lock file in place so every process locks the same file. The OS
    releases the lock automatically if a launcher exits or is killed.
    """
    lock_dir = base / ".ucode"
    lock_dir.mkdir(parents=True, exist_ok=True)
    with (lock_dir / "packaged-skills.lock").open("a+b") as lock_file:
        _lock_file(lock_file, timeout_seconds)
        try:
            yield
        finally:
            _unlock_file(lock_file)


def _bundle_digest(skill_dir: Path) -> str | None:
    """Digest a skill's paths and contents, or return None for an invalid bundle."""
    if skill_dir.is_symlink() or not skill_dir.is_dir():
        return None

    digest = hashlib.sha256()
    try:
        for path in sorted(
            skill_dir.rglob("*"), key=lambda item: item.relative_to(skill_dir).as_posix()
        ):
            if path.is_symlink():
                return None
            relative = path.relative_to(skill_dir).as_posix().encode()
            if path.is_dir():
                digest.update(b"d\0" + relative + b"\0")
            elif path.is_file():
                content_digest = hashlib.sha256(path.read_bytes()).digest()
                digest.update(b"f\0" + relative + b"\0" + content_digest)
            else:
                return None
    except OSError:
        return None
    return digest.hexdigest()


def _swap_skill(staged: Path, destination: Path, backup: Path) -> None:
    """Move a staged skill into place, restoring the prior skill on failure."""
    had_destination = _path_exists(destination)
    if had_destination:
        destination.replace(backup)
    try:
        staged.replace(destination)
    except BaseException:
        if had_destination:
            try:
                backup.replace(destination)
            except OSError as rollback_error:
                raise RuntimeError(
                    f"Could not install `{destination}` or restore its previous contents; "
                    f"the backup is at `{backup}`."
                ) from rollback_error
        raise


def _replace_skill(source: Path, destination: Path) -> None:
    """Copy a complete bundle to staging before swapping it into place."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_root = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    staged = temporary_root / "staged"
    backup = temporary_root / "previous"
    completed = False
    try:
        shutil.copytree(source, staged)
        _swap_skill(staged, destination, backup)
        completed = True
    finally:
        # Preserve the backup for manual recovery only when rollback itself failed.
        if completed or not _path_exists(backup):
            shutil.rmtree(temporary_root, ignore_errors=True)


def install_packaged_skills(skill_name: str, home: Path | None = None) -> list[Path]:
    """Install one packaged skill into each harness, replacing only changed bundles."""
    _validate_skill_name(skill_name)
    source = _skills_source() / skill_name
    if not (source / "SKILL.md").is_file():
        raise RuntimeError(f"Unity Gateway's packaged `{skill_name}` skill resource is missing.")
    source_digest = _bundle_digest(source)
    if source_digest is None:
        raise RuntimeError(f"Unity Gateway's packaged `{skill_name}` skill resource is invalid.")

    base = Path.home() if home is None else home
    installed: list[Path] = []
    with _installation_lock(base):
        for root in _SKILL_ROOTS:
            destination = base / root / skill_name
            if _bundle_digest(destination) != source_digest:
                _replace_skill(source, destination)
            installed.append(destination)
    return installed


def uninstall_packaged_skill(skill_name: str, home: Path | None = None) -> list[Path]:
    """Remove one packaged skill from each harness's global skill directory."""
    _validate_skill_name(skill_name)

    base = Path.home() if home is None else home
    removed: list[Path] = []
    with _installation_lock(base):
        for root in _SKILL_ROOTS:
            destination = base / root / skill_name
            if _remove_skill_path(destination):
                removed.append(destination)
    return removed
