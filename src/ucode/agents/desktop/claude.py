"""Claude Desktop third-party profile configuration.

Claude Desktop keeps its third-party provider profiles outside the normal Claude Code
configuration. This module manages the workspace-derived UUID profile, plus the ``appliedId``
selection while that selection still points at the Unity Gateway profile. Optional custom headers
are written only when supplied.

Profile rendering and file writes live in ``claude_config``; ownership records and
revert policy live in ``claude_state``.

Native paths follow Claude Desktop's macOS, Windows, and Linux application-data layout.
"""

from __future__ import annotations

import copy
import os
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ucode.agents.desktop import claude_config as desktop_config
from ucode.agents.desktop import claude_state as desktop_state
from ucode.agents.desktop.claude_config import (
    ALL_PROFILE_KEYS,
    META_FILENAME,
    OPTIONAL_PROFILE_KEYS,
    OWNED_PROFILE_KEYS,
    PROFILE_NAME,
    ClaudeDesktopConflictError,
    ClaudeDesktopError,
    ClaudeDesktopUnavailableError,
    ClaudeDesktopUnsupportedError,
    render_profile,
)
from ucode.agents.desktop.claude_state import (
    MANIFEST_FILENAME,
    MANIFEST_VERSION,
    PROFILE_NAMESPACE,
)
from ucode.config_io import APP_DIR
from ucode.workspace_urls import canonical_workspace_origin

__all__ = [
    "ALL_PROFILE_KEYS",
    "DEFAULT_MODEL_WARNING",
    "MANIFEST_FILENAME",
    "MANIFEST_VERSION",
    "META_FILENAME",
    "OPTIONAL_PROFILE_KEYS",
    "OWNED_PROFILE_KEYS",
    "PROFILE_NAME",
    "PROFILE_NAMESPACE",
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
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class RevertResult:
    """Result of reverting the recorded Unity Gateway profile."""

    profile_id: str | None
    changed: bool
    drifted: bool = False
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Paths:
    directory: Path
    metadata: Path
    state: Path
    lock: Path
    metadata_was_absent: bool
    directory_was_absent: bool


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
    workspace: str,
    *,
    model_ids: Sequence[str],
    base_url: str,
    helper_command: str | Path,
    helper_args: Sequence[str],
    custom_headers: Mapping[str, str] | None = None,
    state_path: Path | str | None = None,
    profile_key: str | None = None,
) -> ConfigureResult:
    """Create or update the dedicated Unity Gateway profile and make it applied.

    The workspace-derived UUID identifies the managed profile. Bookkeeping retains the original
    baseline for revert; existing random-ID profiles are left alone.
    """

    if not isinstance(workspace, str) or not workspace:
        raise ValueError("Claude Desktop workspace must be a non-empty string.")
    workspace = canonical_workspace_origin(workspace)
    rendered = render_profile(
        model_ids=model_ids,
        base_url=base_url,
        helper_command=helper_command,
        helper_args=helper_args,
        custom_headers=custom_headers,
    )
    paths = _resolve_paths(state_path=state_path)
    with desktop_config.locked(paths.lock):
        metadata_original = desktop_config.read_optional_object(
            paths.metadata, "Claude Desktop metadata"
        )
        metadata = desktop_config.validate_metadata(
            metadata_original if metadata_original is not None else desktop_config.new_metadata(),
            paths.metadata,
        )
        state_was_absent = not paths.state.exists()
        manifest = desktop_state.read_manifest(paths.state)
        profile_id = str(uuid.uuid5(PROFILE_NAMESPACE, workspace))
        recorded_scope, record = desktop_state.find_workspace_record(
            manifest, paths.directory, workspace, profile_id=profile_id
        )
        scope = recorded_scope or desktop_state.scope_key(paths.directory, profile_id)
        entry_after: desktop_config.ProfileEntry = {
            "id": profile_id,
            "name": f"{PROFILE_NAME} - {profile_key}" if profile_key else PROFILE_NAME,
        }
        profile_path = desktop_config.profile_path(paths.directory, profile_id)
        profile_original = desktop_config.read_optional_object(
            profile_path, "Unity Gateway profile"
        )
        manifest_after = copy.deepcopy(manifest)
        profile_after = desktop_state.merge_owned_profile(
            profile_original or {}, {**(record["owned_after"] if record else {}), **rendered}
        )
        owned_keys = desktop_state.owned_keys_for_rendered(
            rendered, previous=record["owned_keys"] if record else None
        )
        if record is None:
            record = desktop_state.new_record(
                directory=paths.directory,
                workspace=workspace,
                profile_key=profile_key,
                profile_id=profile_id,
                profile_before=profile_original,
                profile_after=profile_after,
                entry_after=entry_after,
                entry_before=next(
                    (entry for entry in metadata["entries"] if entry["id"] == profile_id), None
                ),
                owned_keys=owned_keys,
                prior_applied_present="appliedId" in metadata,
                prior_applied=metadata.get("appliedId"),
            )
            record["metadata_was_absent"] = metadata_original is None
        else:
            record = dict(record)
            if profile_original is None and metadata.get("appliedId") != profile_id:
                record["prior_applied_present"] = "appliedId" in metadata
                record["prior_applied"] = metadata.get("appliedId")
            record["owned_keys"] = owned_keys
            record["owned_after"] = desktop_state.owned_values(profile_after, owned_keys)
            record["profile_after"] = copy.deepcopy(profile_after)

        record["workspace"] = workspace
        record["profile_key"] = profile_key or "default"
        record["entry_after"] = entry_after
        manifest_after["profiles"][scope] = record
        metadata_after = desktop_state.apply_metadata(metadata, profile_id, entry_after)

        profile_changed = profile_original != profile_after
        metadata_changed = metadata_original != metadata_after
        manifest_changed = manifest != manifest_after
        if not (profile_changed or metadata_changed or manifest_changed):
            return ConfigureResult(
                profile_id=profile_id,
                profile_path=profile_path,
                metadata_path=paths.metadata,
                changed=False,
                warnings=(DEFAULT_MODEL_WARNING,),
            )

        desktop_config.write_configuration(
            profile_path=profile_path,
            profile_before=profile_original,
            profile_after=profile_after,
            metadata_path=paths.metadata,
            metadata_before=metadata_original,
            metadata_after=metadata_after,
            state_path=paths.state,
            state_before=None if state_was_absent else manifest,
            state_after=manifest_after,
        )
        return ConfigureResult(
            profile_id=profile_id,
            profile_path=profile_path,
            metadata_path=paths.metadata,
            changed=True,
            warnings=(DEFAULT_MODEL_WARNING,),
        )


def revert_claude_desktop(
    workspace: str,
    *,
    state_path: Path | str | None = None,
    profile_key: str | None = None,
) -> RevertResult:
    """Revert only the recorded Unity Gateway profile fields.

    User edits made after configure win.  In particular, ``appliedId`` is restored only while it
    still points at the Unity Gateway profile; a user's later profile selection is preserved.
    """

    if not isinstance(workspace, str) or not workspace:
        raise ValueError("Claude Desktop workspace must be a non-empty string.")
    workspace = canonical_workspace_origin(workspace)
    manifest_path = Path(state_path) if state_path is not None else APP_DIR / MANIFEST_FILENAME
    if not manifest_path.exists():
        return RevertResult(profile_id=None, changed=False)
    paths = _resolve_paths(state_path=manifest_path)
    with desktop_config.locked(paths.lock):
        manifest = desktop_state.read_manifest(paths.state)
        scope, record = desktop_state.find_workspace_record(manifest, paths.directory, workspace)
        if record is None:
            return RevertResult(profile_id=None, changed=False)
        metadata = desktop_config.validate_metadata(
            desktop_config.read_required_object(paths.metadata, "Claude Desktop metadata"),
            paths.metadata,
        )

        profile_id = record["profile_id"]
        profile_path = desktop_config.profile_path(paths.directory, profile_id)
        profile_original = desktop_config.read_optional_object(
            profile_path, "Unity Gateway profile"
        )
        profile_after, profile_changed, profile_drifted = desktop_state.revert_profile(
            profile_original,
            record,
            profile_path,
        )
        entry = next((item for item in metadata["entries"] if item["id"] == profile_id), None)
        if entry is not None and entry != record["entry_after"]:
            profile_after = profile_original
            profile_changed = False
            profile_drifted = True
        metadata_after, metadata_changed = desktop_state.revert_metadata(
            metadata, record, directory=paths.directory, keep_entry=profile_after is not None
        )
        if record.get("metadata_was_absent") and metadata_after == {"entries": []}:
            metadata_after = None
            metadata_changed = True

        manifest_after = copy.deepcopy(manifest)
        manifest_after["profiles"].pop(scope, None)
        manifest_changed = manifest_after != manifest
        if not (profile_changed or metadata_changed or manifest_changed):
            return RevertResult(
                profile_id=profile_id,
                changed=False,
                drifted=profile_drifted,
            )

        desktop_config.write_configuration(
            profile_path=profile_path,
            profile_before=profile_original,
            profile_after=profile_after,
            metadata_path=paths.metadata,
            metadata_before=metadata,
            metadata_after=metadata_after,
            state_path=paths.state,
            state_before=manifest,
            state_after=manifest_after,
        )
        return RevertResult(
            profile_id=profile_id,
            changed=True,
            drifted=profile_drifted,
        )


DEFAULT_MODEL_WARNING = "Claude Desktop default-model selection is not configured by Unity Gateway."


def _resolve_paths(*, state_path: Path | str | None) -> _Paths:
    resolved_directory = claude_desktop_directory()
    if resolved_directory is None:
        raise ClaudeDesktopUnsupportedError(
            "Claude Desktop profile configuration is unavailable on this platform."
        )
    directory_was_absent = False
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
        directory_was_absent = True
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
    resolved_state = Path(state_path) if state_path is not None else APP_DIR / MANIFEST_FILENAME
    return _Paths(
        directory=resolved_directory,
        metadata=resolved_directory / META_FILENAME,
        state=resolved_state,
        lock=resolved_state.with_name(f".{resolved_state.name}.lock"),
        metadata_was_absent=not (resolved_directory / META_FILENAME).exists(),
        directory_was_absent=directory_was_absent,
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
