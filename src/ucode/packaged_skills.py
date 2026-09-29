"""Install the skills packaged with Unity Gateway."""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
from importlib.metadata import distribution
from pathlib import Path

_SKILL_ROOTS = (".claude/skills", ".agents/skills")
_SKILL_NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
SMART_ROUTER_SKILL = "smart-router"


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


def _replace_skill(source: Path, destination: Path) -> None:
    """Stage a complete bundle, then swap it in while retaining a rollback copy."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_root = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    staged = temporary_root / "staged"
    backup = temporary_root / "previous"
    preserve_temporary_root = False
    try:
        shutil.copytree(source, staged)
        had_destination = destination.exists() or destination.is_symlink()
        if had_destination:
            destination.replace(backup)
        try:
            staged.replace(destination)
        except BaseException:
            if had_destination:
                try:
                    backup.replace(destination)
                except OSError as rollback_error:
                    preserve_temporary_root = True
                    raise RuntimeError(
                        f"Could not install `{destination}` or restore its previous contents; "
                        f"the backup is at `{backup}`."
                    ) from rollback_error
            raise
    finally:
        if not preserve_temporary_root:
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
    for root in _SKILL_ROOTS:
        destination = base / root / skill_name
        if _remove_skill_path(destination):
            removed.append(destination)
    return removed
