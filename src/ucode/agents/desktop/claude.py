"""Manage UG's dedicated Claude Desktop profile and its shared metadata entry."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ucode.agents.desktop import claude_config as desktop_config
from ucode.agents.desktop import claude_state as desktop_state
from ucode.agents.desktop.claude_config import (
    META_FILENAME,
    PROFILE_ID,
    PROFILE_NAME,
    ClaudeDesktopConflictError,
    ClaudeDesktopError,
    ClaudeDesktopUnavailableError,
    ClaudeDesktopUnsupportedError,
    render_profile,
)
from ucode.agents.desktop.claude_state import STATE_FILENAME
from ucode.config_io import APP_DIR

__all__ = [
    "STATE_FILENAME",
    "META_FILENAME",
    "PROFILE_NAME",
    "PROFILE_ID",
    "ClaudeDesktopConflictError",
    "ClaudeDesktopError",
    "ClaudeDesktopUnavailableError",
    "ClaudeDesktopUnsupportedError",
    "ConfigureResult",
    "RevertResult",
    "claude_desktop_directory",
    "claude_desktop_root_directory",
    "configure_claude_desktop",
    "render_profile",
    "revert_claude_desktop",
]


@dataclass(frozen=True)
class ConfigureResult:
    """Result of applying the Unity Gateway profile."""

    profile_id: str
    profile_path: Path
    metadata_path: Path
    changed: bool


@dataclass(frozen=True)
class RevertResult:
    """Result of removing the dedicated Unity Gateway profile."""

    profile_id: str | None
    changed: bool


@dataclass(frozen=True)
class _Paths:
    directory: Path
    metadata: Path
    state: Path
    lock: Path


def claude_desktop_root_directory(
    *, home: Path | str | None = None, platform: str | None = None
) -> Path | None:
    """Return the documented Claude-3p application-data root."""

    platform_name = (platform if platform is not None else sys.platform).lower()
    if platform_name not in ("darwin", "win32", "linux"):
        return None
    home_path = Path(home) if home is not None else Path.home()
    if platform_name == "darwin":
        return home_path / "Library" / "Application Support" / "Claude-3p"
    if platform_name == "linux":
        return home_path / ".config" / "Claude-3p"
    local_app_data = os.environ.get("LOCALAPPDATA")
    return (
        Path(local_app_data) if local_app_data else home_path / "AppData" / "Local"
    ) / "Claude-3p"


def claude_desktop_directory(
    *, home: Path | str | None = None, platform: str | None = None
) -> Path | None:
    """Return the native configLibrary store for UUID profiles and metadata."""

    root = claude_desktop_root_directory(home=home, platform=platform)
    return root / "configLibrary" if root is not None else None


def configure_claude_desktop(
    *,
    model_ids: Sequence[str],
    base_url: str,
    helper_command: str | Path,
    helper_args: Sequence[str],
    custom_headers: Mapping[str, str] | None = None,
    state_path: Path | str | None = None,
    profile_key: str | None = None,
) -> ConfigureResult:
    """Replace UG's reserved profile, register it, and select it."""

    rendered = render_profile(
        model_ids=model_ids,
        base_url=base_url,
        helper_command=helper_command,
        helper_args=helper_args,
        custom_headers=custom_headers,
    )
    paths = _resolve_paths(state_path=state_path)
    profile_path = paths.directory / f"{PROFILE_ID}.json"
    with desktop_config.locked(paths.lock):
        profile_before = desktop_config.read_optional_bytes(profile_path)
        metadata_before = desktop_config.read_optional_bytes(paths.metadata)
        state_before = desktop_config.read_optional_bytes(paths.state)
        metadata = desktop_config.validate_metadata(
            desktop_config.parse_object(metadata_before, paths.metadata, "Claude Desktop metadata")
            if metadata_before is not None
            else {"entries": []},
            paths.metadata,
        )
        prior_applied = (
            desktop_state.previous_selection(state_before, paths.state)
            if metadata.get("appliedId") == PROFILE_ID
            else metadata.get("appliedId")
        )
        changed = desktop_config.write_configuration(
            [
                (profile_path, profile_before, rendered),
                (
                    paths.metadata,
                    metadata_before,
                    desktop_state.apply_metadata(metadata, profile_key=profile_key),
                ),
                (paths.state, state_before, {"prior_applied": prior_applied}),
            ]
        )
        return ConfigureResult(
            profile_id=PROFILE_ID,
            profile_path=profile_path,
            metadata_path=paths.metadata,
            changed=changed,
        )


def revert_claude_desktop(*, state_path: Path | str | None = None) -> RevertResult:
    """Delete UG's reserved profile and registration, preserving unrelated profiles."""

    directory = claude_desktop_directory()
    if directory is None or not directory.exists():
        return RevertResult(profile_id=None, changed=False)
    paths = _resolve_paths(state_path=state_path)
    profile_path = paths.directory / f"{PROFILE_ID}.json"
    with desktop_config.locked(paths.lock):
        profile_before = desktop_config.read_optional_bytes(profile_path)
        metadata_before = desktop_config.read_optional_bytes(paths.metadata)
        state_before = desktop_config.read_optional_bytes(paths.state)
        metadata_after = None
        if metadata_before is not None:
            metadata = desktop_config.validate_metadata(
                desktop_config.parse_object(
                    metadata_before, paths.metadata, "Claude Desktop metadata"
                ),
                paths.metadata,
            )
            metadata_after = desktop_state.revert_metadata(
                metadata,
                prior_applied=desktop_state.previous_selection(state_before, paths.state),
                directory=paths.directory,
            )
        changed = desktop_config.write_configuration(
            [
                (profile_path, profile_before, None),
                (paths.metadata, metadata_before, metadata_after),
                (paths.state, state_before, None),
            ]
        )
        return RevertResult(profile_id=PROFILE_ID if changed else None, changed=changed)


def _resolve_paths(*, state_path: Path | str | None) -> _Paths:
    resolved_directory = claude_desktop_directory()
    if resolved_directory is None:
        raise ClaudeDesktopUnsupportedError(
            "Claude Desktop profile configuration is unavailable on this platform."
        )
    if not resolved_directory.exists():
        can_initialize = resolved_directory.parent.is_dir()
        if sys.platform == "darwin":
            can_initialize = _claude_desktop_installed()
        if not can_initialize:
            raise ClaudeDesktopUnavailableError(
                f"Claude Desktop profile directory is unavailable at {resolved_directory}. "
                "Install/open Claude Desktop once, then run configure again; Unity Gateway will not "
                "install it."
            )
        try:
            resolved_directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ClaudeDesktopUnavailableError(
                f"Could not initialize Claude Desktop profile directory at {resolved_directory}."
            ) from exc
    if not resolved_directory.is_dir():
        raise ClaudeDesktopUnavailableError(
            f"Claude Desktop profile path is not a directory: {resolved_directory}."
        )
    resolved_state = Path(state_path) if state_path is not None else APP_DIR / STATE_FILENAME
    return _Paths(
        directory=resolved_directory,
        metadata=resolved_directory / META_FILENAME,
        state=resolved_state,
        lock=resolved_state.with_name(f".{resolved_state.name}.lock"),
    )


def _claude_desktop_installed(*, home: Path | str | None = None) -> bool:
    """Best-effort macOS app presence check used before creating a native profile directory."""

    home_path = Path(home) if home is not None else Path.home()
    return any(
        path.is_dir()
        for path in (
            Path("/Applications/Claude.app"),
            home_path / "Applications" / "Claude.app",
        )
    )
