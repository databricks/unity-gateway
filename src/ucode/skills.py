"""Install skills shipped with Unity Gateway."""

from __future__ import annotations

import hashlib
import re
import shutil
from importlib.metadata import distribution
from pathlib import Path

_SKILL_ROOTS = {"claude": ".claude/skills", "codex": ".codex/skills"}
_SKILL_NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
SMART_ROUTER_SKILL = "smart-router"


def _skills_source() -> Path:
    installed = Path(str(distribution("unity-gateway").locate_file("skills")))
    if installed.is_dir():
        return installed
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
        for path in sorted(skill_dir.rglob("*")):
            if path.is_symlink():
                return None
            if path.is_file():
                kind = b"f"
                content = hashlib.sha256(path.read_bytes()).digest()
            elif path.is_dir():
                kind = b"d"
                content = b""
            else:
                return None
            relative = path.relative_to(skill_dir).as_posix().encode()
            digest.update(kind + b"\0" + relative + b"\0" + content)
    except OSError:
        return None
    return digest.hexdigest()


def install_skill(skill_name: str, agent: str, home: Path | None = None) -> Path:
    """Install one skill for an agent, replacing only a changed bundle."""
    _validate_skill_name(skill_name)
    try:
        root = _SKILL_ROOTS[agent]
    except KeyError:
        raise ValueError(f"Unsupported skill agent: {agent!r}") from None
    source = _skills_source() / skill_name
    if not (source / "SKILL.md").is_file():
        raise RuntimeError(f"Unity Gateway's `{skill_name}` skill resource is missing.")
    source_digest = _bundle_digest(source)
    if source_digest is None:
        raise RuntimeError(f"Unity Gateway's `{skill_name}` skill resource is invalid.")

    base = Path.home() if home is None else home
    destination = base / root / skill_name
    if _bundle_digest(destination) != source_digest:
        _remove_skill_path(destination)
        shutil.copytree(source, destination)
    return destination


def uninstall_skill(skill_name: str, home: Path | None = None) -> list[Path]:
    """Remove one skill from each agent's global skill directory."""
    _validate_skill_name(skill_name)

    base = Path.home() if home is None else home
    removed: list[Path] = []
    for root in _SKILL_ROOTS.values():
        destination = base / root / skill_name
        if _remove_skill_path(destination):
            removed.append(destination)
    return removed
