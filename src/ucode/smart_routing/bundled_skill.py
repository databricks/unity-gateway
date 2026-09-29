"""Install and safely remove ug's bundled Smart Router skill."""

from __future__ import annotations

import hashlib
import shlex
import shutil
from importlib import resources
from pathlib import Path

from ucode import config_io
from ucode.config_io import atomic_write_json
from ucode.databricks import ug_binary
from ucode.ui import print_warning

SKILL_NAME = "smart-router"
MANIFEST_VERSION = 1
_SKILL_RELATIVE_PATHS = (".claude/skills", ".agents/skills")
_UG_EXECUTABLE_MARKER = "__UG_EXECUTABLE__"
_UG_LAUNCHER_MARKER = "__UG_LAUNCHER__"


def _manifest_path() -> Path:
    return config_io.APP_DIR / "bundled-skills.json"


def _skill_content(launcher: str = "claude") -> str:
    if launcher not in {"claude", "codex"}:
        raise ValueError(f"unsupported Smart Router skill launcher: {launcher}")
    template = (
        resources.files("ucode")
        .joinpath("bundled_skills", SKILL_NAME, "SKILL.md")
        .read_text(encoding="utf-8")
    )
    return template.replace(_UG_EXECUTABLE_MARKER, shlex.quote(ug_binary())).replace(
        _UG_LAUNCHER_MARKER, launcher
    )


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _load_manifest() -> dict[str, str]:
    data = config_io.read_json_safe(_manifest_path())
    installs = data.get("installs")
    if data.get("version") != MANIFEST_VERSION or not isinstance(installs, dict):
        return {}
    return {
        path: digest
        for path, digest in installs.items()
        if isinstance(path, str) and isinstance(digest, str)
    }


def _save_manifest(installs: dict[str, str]) -> None:
    if installs:
        atomic_write_json(_manifest_path(), {"version": MANIFEST_VERSION, "installs": installs})
    else:
        _manifest_path().unlink(missing_ok=True)


def _installed_unchanged(skill_dir: Path, expected_digest: str) -> bool:
    skill_file = skill_dir / "SKILL.md"
    try:
        if skill_dir.is_symlink():
            return False
        entries = list(skill_dir.iterdir())
        return (
            len(entries) == 1
            and entries[0].name == "SKILL.md"
            and skill_file.is_file()
            and _digest(skill_file.read_text(encoding="utf-8")) == expected_digest
        )
    except (OSError, UnicodeError):
        return False


def install_bundled_skill(home: Path | None = None) -> list[Path]:
    """Install or upgrade unchanged ug-owned copies; preserve every collision."""
    base = config_io.APP_DIR.parent if home is None else home
    installs = _load_manifest()
    installed: list[Path] = []
    for relative_root in _SKILL_RELATIVE_PATHS:
        launcher = "claude" if relative_root == ".claude/skills" else "codex"
        content = _skill_content(launcher)
        content_digest = _digest(content)
        skill_dir = base / relative_root / SKILL_NAME
        key = str(skill_dir)
        prior_digest = installs.get(key)
        if (skill_dir.exists() or skill_dir.is_symlink()) and (
            prior_digest is None or not _installed_unchanged(skill_dir, prior_digest)
        ):
            print_warning(
                f"Kept existing `{skill_dir}`; install the bundled Smart Router skill "
                "manually if you want to replace it."
            )
            continue
        try:
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
        except OSError as exc:
            print_warning(f"Could not install the Smart Router skill at `{skill_dir}`: {exc}")
            continue
        installs[key] = content_digest
        installed.append(skill_dir)
    try:
        _save_manifest(installs)
    except RuntimeError as exc:
        print_warning(f"Could not record Smart Router skill ownership: {exc}")
    return installed


def revert_bundled_skill() -> dict[Path, bool]:
    """Remove only byte-for-byte unchanged directories attributed to ug."""
    installs = _load_manifest()
    results: dict[Path, bool] = {}
    remaining: dict[str, str] = {}
    for raw_path, expected_digest in installs.items():
        skill_dir = Path(raw_path)
        unchanged = _installed_unchanged(skill_dir, expected_digest)
        if unchanged:
            try:
                shutil.rmtree(skill_dir)
            except OSError as exc:
                print_warning(f"Could not remove the Smart Router skill at `{skill_dir}`: {exc}")
                unchanged = False
                remaining[raw_path] = expected_digest
        results[skill_dir] = unchanged
    try:
        _save_manifest(remaining)
    except RuntimeError as exc:
        print_warning(f"Could not update Smart Router skill ownership: {exc}")
    return results
