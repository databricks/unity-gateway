"""Install Unity Gateway's Smart Router skill for Claude Code and Codex."""

from __future__ import annotations

import shutil
from importlib.metadata import distribution
from pathlib import Path

SKILL_NAME = "smart-router"
_SKILL_ROOTS = (".claude/skills", ".agents/skills")


def _skill_source() -> Path:
    packaged = Path(str(distribution("unity-gateway").locate_file(f"skills/{SKILL_NAME}")))
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parents[2] / "skills" / SKILL_NAME


def install_smart_router_skill(home: Path | None = None) -> list[Path]:
    """Copy the packaged skill into each harness's global skill directory."""
    source = _skill_source()
    if not (source / "SKILL.md").is_file():
        raise RuntimeError("Unity Gateway's Smart Router skill resource is missing.")

    base = Path.home() if home is None else home
    installed = [base / root / SKILL_NAME for root in _SKILL_ROOTS]
    for destination in installed:
        shutil.copytree(source, destination, dirs_exist_ok=True)
    return installed
