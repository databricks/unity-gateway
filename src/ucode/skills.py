"""Install skills shipped with Unity Gateway."""

from __future__ import annotations

import hashlib
import re
import shutil
from importlib.metadata import distribution
from pathlib import Path

_SKILL_ROOTS = {"claude": ".claude/skills", "codex": ".codex/skills"}
_LEGACY_SKILL_ROOTS = (".agents/skills",)
_SKILL_NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
SMART_ROUTER_SKILL = "smart-router"
# Install the delegation workflow in the same eligible sessions as Smart Router.
SMART_ROUTER_ORCHESTRATOR_SKILL = "smart-router-orchestrator"


def _skills_source() -> Path:
    installed = Path(str(distribution("unity-gateway").locate_file("skills")))
    if installed.is_dir():
        return installed
    return Path(__file__).resolve().parents[2] / "skills"


def _validate_skill_name(skill_name: str) -> None:
    if _SKILL_NAME_PATTERN.fullmatch(skill_name) is None:
        raise ValueError(f"Invalid skill name: {skill_name!r}")


def skill_entrypoint(skill_dir: Path, agent: str) -> Path:
    entrypoint = skill_dir / f"SKILL.{agent}.md"
    return entrypoint if entrypoint.is_file() else skill_dir / "SKILL.md"


def _remove_skill_path(destination: Path) -> bool:
    """Remove an existing skill path so an install is an exact replacement."""
    if destination.is_symlink() or destination.is_file():
        destination.unlink()
        return True
    if destination.is_dir():
        shutil.rmtree(destination)
        return True
    return False


def _bundle_digest(skill_dir: Path, *, entrypoint: Path | None = None) -> str | None:
    """Digest a skill's paths and contents, or return None for an invalid bundle."""
    if skill_dir.is_symlink() or not skill_dir.is_dir():
        return None

    digest = hashlib.sha256()
    try:
        paths = {path.relative_to(skill_dir): path for path in skill_dir.rglob("*")}
        if entrypoint is not None:
            # Compare the bundle as installed, with the selected instructions at SKILL.md.
            paths[Path("SKILL.md")] = entrypoint
        for relative, path in sorted(paths.items()):
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
            digest.update(kind + b"\0" + relative.as_posix().encode() + b"\0" + content)
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
    entrypoint = skill_entrypoint(source, agent)
    if not entrypoint.is_file():
        raise RuntimeError(f"Unity Gateway's `{skill_name}` skill resource is missing.")
    source_digest = _bundle_digest(source, entrypoint=entrypoint)
    if source_digest is None:
        raise RuntimeError(f"Unity Gateway's `{skill_name}` skill resource is invalid.")

    base = Path.home() if home is None else home
    destination = base / root / skill_name
    if _bundle_digest(destination) != source_digest:
        _remove_skill_path(destination)
        shutil.copytree(source, destination)
        shutil.copy2(entrypoint, destination / "SKILL.md")
    return destination


def uninstall_skill(skill_name: str, home: Path | None = None) -> list[Path]:
    """Remove one skill from current and legacy global skill directories."""
    _validate_skill_name(skill_name)

    base = Path.home() if home is None else home
    roots = (*_SKILL_ROOTS.values(), *_LEGACY_SKILL_ROOTS)

    removed: list[Path] = []
    for root in roots:
        destination = base / root / skill_name
        if _remove_skill_path(destination):
            removed.append(destination)
    return removed
