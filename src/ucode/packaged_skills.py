"""Install the skills packaged with Unity Gateway."""

from __future__ import annotations

import re
import shutil
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


def install_packaged_skills(skill_name: str, home: Path | None = None) -> list[Path]:
    """Copy one packaged skill into each harness's global skill directory."""
    _validate_skill_name(skill_name)
    source = _skills_source() / skill_name
    if not (source / "SKILL.md").is_file():
        raise RuntimeError(f"Unity Gateway's packaged `{skill_name}` skill resource is missing.")

    base = Path.home() if home is None else home
    installed: list[Path] = []
    for root in _SKILL_ROOTS:
        destination = base / root / skill_name
        shutil.copytree(source, destination, dirs_exist_ok=True)
        installed.append(destination)
    return installed


def uninstall_packaged_skill(skill_name: str, home: Path | None = None) -> list[Path]:
    """Remove one packaged skill from each harness's global skill directory."""
    _validate_skill_name(skill_name)

    base = Path.home() if home is None else home
    removed: list[Path] = []
    for root in _SKILL_ROOTS:
        destination = base / root / skill_name
        if destination.is_symlink() or destination.is_file():
            destination.unlink()
            removed.append(destination)
        elif destination.is_dir():
            shutil.rmtree(destination)
            removed.append(destination)
    return removed
