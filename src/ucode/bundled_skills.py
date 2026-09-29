"""Install and safely remove the skills bundled with Unity Gateway."""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
from collections.abc import Callable, Mapping
from importlib.metadata import distribution
from pathlib import Path, PurePosixPath

from ucode import config_io
from ucode.config_io import atomic_write_json
from ucode.ui import print_warning

MANIFEST_VERSION = 2
_LEGACY_MANIFEST_VERSION = 1
_INSTALL_TARGETS = ((".claude/skills", "claude"), (".agents/skills", "codex"))
_SKILL_NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")

Bundle = dict[str, bytes | None]
SkillRenderer = Callable[[str, str, bytes], bytes]


def _manifest_path() -> Path:
    return config_io.APP_DIR / "bundled-skills.json"


def _skills_root() -> Path:
    return Path(str(distribution("unity-gateway").locate_file("skills")))


def _skill_sources() -> list[Path]:
    root = _skills_root()
    if not root.is_dir():
        raise RuntimeError("Unity Gateway's bundled skill resources are missing.")
    skills: list[Path] = []
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        if not entry.is_dir():
            continue
        if entry.is_symlink():
            print_warning(f"Skipping symlinked bundled skill `{entry.name}`.")
        elif len(entry.name) > 64 or _SKILL_NAME_PATTERN.fullmatch(entry.name) is None:
            print_warning(f"Skipping invalid bundled skill name `{entry.name}`.")
        elif not entry.joinpath("SKILL.md").is_file():
            print_warning(f"Skipping bundled skill `{entry.name}` without a SKILL.md.")
        else:
            skills.append(entry)
    return skills


def _source_files(directory: Path) -> Bundle:
    files: Bundle = {}
    for entry in sorted(directory.rglob("*")):
        relative = entry.relative_to(directory)
        if entry.is_symlink():
            raise RuntimeError(f"Bundled skill contains a symlink: {relative}")
        if entry.is_file():
            files[relative.as_posix()] = entry.read_bytes()
    return files


def _rendered_bundle(
    source: Path,
    launcher: str,
    renderer: SkillRenderer | None,
) -> Bundle:
    if launcher not in {"claude", "codex"}:
        raise ValueError(f"unsupported skill launcher: {launcher}")
    files = _source_files(source)
    skill = files.get("SKILL.md")
    if not isinstance(skill, bytes):
        raise RuntimeError(f"Bundled skill `{source.name}` has no SKILL.md.")
    if renderer is not None:
        files["SKILL.md"] = renderer(source.name, launcher, skill)
    for relative_path in tuple(files):
        parent = PurePosixPath(relative_path).parent
        while parent != PurePosixPath("."):
            files.setdefault(parent.as_posix(), None)
            parent = parent.parent
    return files


def _digest(bundle: Mapping[str, bytes | None]) -> str:
    digest = hashlib.sha256()
    for relative_path, content in sorted(bundle.items()):
        digest.update(relative_path.encode())
        digest.update(b"\0d\0" if content is None else b"\0f\0")
        if content is not None:
            digest.update(content)
    return digest.hexdigest()


def _installed_bundle(skill_dir: Path) -> Bundle | None:
    if skill_dir.is_symlink() or not skill_dir.is_dir():
        return None
    bundle: Bundle = {}
    try:
        for path in skill_dir.rglob("*"):
            if path.is_symlink():
                return None
            relative = path.relative_to(skill_dir).as_posix()
            if path.is_dir():
                bundle[relative] = None
            elif path.is_file():
                bundle[relative] = path.read_bytes()
    except OSError:
        return None
    return bundle


def _installed_unchanged(
    skill_dir: Path,
    expected_digest: str,
    manifest_version: int = MANIFEST_VERSION,
) -> bool:
    if manifest_version == _LEGACY_MANIFEST_VERSION:
        try:
            entries = list(skill_dir.iterdir())
            return (
                not skill_dir.is_symlink()
                and len(entries) == 1
                and entries[0].name == "SKILL.md"
                and entries[0].is_file()
                and hashlib.sha256(entries[0].read_bytes()).hexdigest() == expected_digest
            )
        except OSError:
            return False
    bundle = _installed_bundle(skill_dir)
    return bundle is not None and _digest(bundle) == expected_digest


def _write_bundle(skill_dir: Path, bundle: Bundle) -> None:
    skill_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{skill_dir.name}-", dir=skill_dir.parent))
    try:
        for relative_path, content in bundle.items():
            destination = staging / relative_path
            if content is None:
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
        if skill_dir.exists():
            shutil.rmtree(skill_dir)
        staging.replace(skill_dir)
    except OSError:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _load_manifest() -> tuple[int, dict[str, str]]:
    data = config_io.read_json_safe(_manifest_path())
    installs = data.get("installs")
    version = data.get("version")
    if version not in {_LEGACY_MANIFEST_VERSION, MANIFEST_VERSION} or not isinstance(
        installs, dict
    ):
        return MANIFEST_VERSION, {}
    return version, {
        path: digest
        for path, digest in installs.items()
        if isinstance(path, str) and isinstance(digest, str)
    }


def _save_manifest(installs: dict[str, str]) -> None:
    if installs:
        atomic_write_json(_manifest_path(), {"version": MANIFEST_VERSION, "installs": installs})
    else:
        _manifest_path().unlink(missing_ok=True)


def install_bundled_skills(
    home: Path | None = None,
    *,
    renderer: SkillRenderer | None = None,
) -> list[Path]:
    """Install every bundled skill while preserving collisions and user edits."""
    base = (config_io.APP_DIR.parent if home is None else home).resolve()
    manifest_version, installs = _load_manifest()
    installed: list[Path] = []
    for source in _skill_sources():
        for relative_root, launcher in _INSTALL_TARGETS:
            bundle = _rendered_bundle(source, launcher, renderer)
            content_digest = _digest(bundle)
            skill_dir = base / relative_root / source.name
            key = str(skill_dir)
            prior_digest = installs.get(key)
            if (skill_dir.exists() or skill_dir.is_symlink()) and (
                prior_digest is None
                or not _installed_unchanged(skill_dir, prior_digest, manifest_version)
            ):
                print_warning(
                    f"Kept existing `{skill_dir}`; install the bundled `{source.name}` skill "
                    "manually if you want to replace it."
                )
                continue
            try:
                _write_bundle(skill_dir, bundle)
            except OSError as exc:
                print_warning(f"Could not install the bundled skill at `{skill_dir}`: {exc}")
                continue
            installs[key] = content_digest
            installed.append(skill_dir)
    try:
        _save_manifest(installs)
    except RuntimeError as exc:
        print_warning(f"Could not record bundled skill ownership: {exc}")
    return installed


def _is_install_target(skill_dir: Path, base: Path) -> bool:
    if len(skill_dir.name) > 64 or _SKILL_NAME_PATTERN.fullmatch(skill_dir.name) is None:
        return False
    resolved_parent = skill_dir.parent.resolve()
    return any(
        resolved_parent == (base / relative_root).resolve() for relative_root, _ in _INSTALL_TARGETS
    )


def revert_bundled_skills(home: Path | None = None) -> dict[Path, bool]:
    """Remove only byte-for-byte unchanged directories attributed to Unity Gateway."""
    base = (config_io.APP_DIR.parent if home is None else home).resolve()
    manifest_version, installs = _load_manifest()
    results: dict[Path, bool] = {}
    remaining: dict[str, str] = {}
    for raw_path, expected_digest in installs.items():
        skill_dir = Path(raw_path)
        if not skill_dir.is_absolute() or not _is_install_target(skill_dir, base):
            print_warning(f"Ignored unsafe bundled skill manifest path `{skill_dir}`.")
            continue
        unchanged = _installed_unchanged(skill_dir, expected_digest, manifest_version)
        if unchanged:
            try:
                shutil.rmtree(skill_dir)
            except OSError as exc:
                print_warning(f"Could not remove the bundled skill at `{skill_dir}`: {exc}")
                unchanged = False
                remaining[raw_path] = expected_digest
        results[skill_dir] = unchanged
    try:
        _save_manifest(remaining)
    except RuntimeError as exc:
        print_warning(f"Could not update bundled skill ownership: {exc}")
    return results
