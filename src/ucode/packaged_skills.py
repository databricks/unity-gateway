"""Install the skills packaged with Unity Gateway."""

from __future__ import annotations

import re
import shutil
from importlib.metadata import distribution
from pathlib import Path

_SKILL_ROOTS = (".claude/skills", ".agents/skills")
_SKILL_NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def _skills_source() -> Path:
    packaged = Path(str(distribution("unity-gateway").locate_file("skills")))
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parents[2] / "skills"


def install_packaged_skills(home: Path | None = None) -> list[Path]:
    """Copy every packaged skill into each harness's global skill directory."""
    source = _skills_source()
    if not source.is_dir():
        raise RuntimeError("Unity Gateway's packaged skill resources are missing.")

    skills = sorted(path for path in source.iterdir() if (path / "SKILL.md").is_file())
    base = Path.home() if home is None else home
    installed: list[Path] = []
    for skill in skills:
        for root in _SKILL_ROOTS:
            destination = base / root / skill.name
            shutil.copytree(skill, destination, dirs_exist_ok=True)
            installed.append(destination)
    return installed


def uninstall_packaged_skill(skill_name: str, home: Path | None = None) -> list[Path]:
    """Remove one packaged skill from each harness's global skill directory."""
    if _SKILL_NAME_PATTERN.fullmatch(skill_name) is None:
        raise ValueError(f"Invalid skill name: {skill_name!r}")

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
