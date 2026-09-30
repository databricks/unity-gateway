"""Install skills shipped with Unity Gateway."""

from __future__ import annotations

import hashlib
import re
import shutil
from importlib.metadata import distribution
from pathlib import Path

_CLAUDE_SKILL_ROOT = ".claude/skills"
_CODEX_SKILL_ROOT = ".codex/skills"
_SHARED_SKILL_ROOT = ".agents/skills"
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


def _is_codex_alias(alias: Path, claude_skill: Path) -> bool:
    """Whether Codex already exposes the Claude skill through plugin sync."""
    if not alias.is_symlink():
        return False
    try:
        return alias.resolve(strict=False) == claude_skill.resolve(strict=False)
    except OSError:
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


def install_skill(skill_name: str, home: Path | None = None) -> list[Path]:
    """Install one skill for Claude and Codex, replacing only changed bundles."""
    _validate_skill_name(skill_name)
    source = _skills_source() / skill_name
    if not (source / "SKILL.md").is_file():
        raise RuntimeError(f"Unity Gateway's `{skill_name}` skill resource is missing.")
    source_digest = _bundle_digest(source)
    if source_digest is None:
        raise RuntimeError(f"Unity Gateway's `{skill_name}` skill resource is invalid.")

    base = Path.home() if home is None else home
    claude_skill = base / _CLAUDE_SKILL_ROOT / skill_name
    codex_alias = base / _CODEX_SKILL_ROOT / skill_name
    shared_skill = base / _SHARED_SKILL_ROOT / skill_name
    codex_uses_alias = _is_codex_alias(codex_alias, claude_skill)

    installed: list[Path] = []
    roots = (
        (_CLAUDE_SKILL_ROOT,)
        if codex_uses_alias
        else (
            _CLAUDE_SKILL_ROOT,
            _SHARED_SKILL_ROOT,
        )
    )
    for root in roots:
        destination = base / root / skill_name
        if _bundle_digest(destination) != source_digest:
            _remove_skill_path(destination)
            shutil.copytree(source, destination)
        installed.append(destination)
    if codex_uses_alias:
        _remove_skill_path(shared_skill)
        installed.append(codex_alias)
    return installed


def uninstall_skill(skill_name: str, home: Path | None = None) -> list[Path]:
    """Remove one skill from every supported global skill directory."""
    _validate_skill_name(skill_name)

    base = Path.home() if home is None else home
    destinations = [
        base / _CLAUDE_SKILL_ROOT / skill_name,
        base / _CODEX_SKILL_ROOT / skill_name,
        base / _SHARED_SKILL_ROOT / skill_name,
    ]

    removed: list[Path] = []
    for destination in destinations:
        if _remove_skill_path(destination):
            removed.append(destination)
    return removed
