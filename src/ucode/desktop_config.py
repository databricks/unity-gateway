"""Opt-in configuration for independently launched Codex and OpenCode desktop apps."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from ucode.agents import codex, opencode
from ucode.config_io import (
    APP_DIR,
    backup_existing_file,
    deep_merge_dict,
    is_dry_run,
    restore_file,
    write_json_file,
    write_text_file,
    write_toml_file,
)
from ucode.databricks import build_opencode_base_urls, get_databricks_token

CODEX_DESKTOP_BACKUP_PATH = APP_DIR / "codex-desktop.backup.toml"
OPENCODE_DESKTOP_CONFIG_PATH = Path.home() / ".config" / "opencode" / "opencode.json"
OPENCODE_DESKTOP_BACKUP_PATH = APP_DIR / "opencode-desktop.backup.json"
OPENCODE_PLUGIN_BACKUP_PATH = APP_DIR / "opencode-desktop-plugin.backup.js"


def _desktop_plugin_path() -> Path:
    return OPENCODE_DESKTOP_CONFIG_PATH.parent / "plugin" / opencode.OPENCODE_AUTH_PLUGIN_PATH.name


def _backup_path(path: Path, backup: Path) -> None:
    """Remember an absent file too, so revert can remove one we created."""
    if is_dry_run() or backup.exists() or backup.with_suffix(backup.suffix + ".created").exists():
        return
    if path.exists():
        backup_existing_file(path, backup)
    else:
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.with_suffix(backup.suffix + ".created").touch()


def _restore_path(path: Path, backup: Path) -> bool:
    marker = backup.with_suffix(backup.suffix + ".created")
    if backup.exists():
        return restore_file(path, backup, False)
    if marker.exists():
        restored = restore_file(path, backup, True)
        marker.unlink()
        return restored
    return False


def revert_desktop_config() -> dict[str, bool]:
    """Restore opt-in desktop files, including an auth plugin replaced by ug."""
    return {
        "codex": _restore_path(codex._legacy_config_path(), CODEX_DESKTOP_BACKUP_PATH),
        "opencode": _restore_path(OPENCODE_DESKTOP_CONFIG_PATH, OPENCODE_DESKTOP_BACKUP_PATH),
        "opencode_plugin": _restore_path(_desktop_plugin_path(), OPENCODE_PLUGIN_BACKUP_PATH),
    }


def backup_desktop_config(tool: str) -> None:
    """Save the user's desktop settings before ug changes any shared file."""
    if tool == "codex":
        _backup_path(codex._legacy_config_path(), CODEX_DESKTOP_BACKUP_PATH)
    elif tool == "opencode":
        _backup_path(OPENCODE_DESKTOP_CONFIG_PATH, OPENCODE_DESKTOP_BACKUP_PATH)
        _backup_path(_desktop_plugin_path(), OPENCODE_PLUGIN_BACKUP_PATH)


def sync_codex_desktop(state: dict) -> None:
    """Apply the managed Codex provider and default to the shared app config."""
    model = state.get("codex_default_model")
    if not isinstance(model, str) or not model:
        raise RuntimeError("Codex Desktop needs a managed default model.")
    path = codex._legacy_config_path()
    overlay = codex.render_overlay(
        state["workspace"],
        model,
        state.get("profile"),
        use_pat=bool(state.get("use_pat")),
        custom_oauth=state.get("custom_oauth"),
        managed_http_headers=state.get("codex_http_headers"),
    )
    doc = codex._read_app_config()
    backup_desktop_config("codex")
    # Keep the app's other providers, MCPs, hooks, and user preferences.
    deep_merge_dict(doc, copy.deepcopy(overlay))
    write_toml_file(path, doc)


def sync_opencode_desktop(state: dict) -> None:
    """Apply the managed model and provider to OpenCode Desktop's usual config."""
    model = state.get("opencode_default_model")
    models = state.get("opencode_models") or {}
    if not isinstance(model, str) or not model or not isinstance(models, dict):
        raise RuntimeError("OpenCode Desktop needs a managed default model and provider models.")
    token = get_databricks_token(state["workspace"], state.get("profile"))
    base_urls = state.get("base_urls", {}).get("opencode") or build_opencode_base_urls(
        state["workspace"]
    )
    overlay, _ = opencode.render_overlay(model, token, base_urls, models)
    if overlay["model"] == model or "provider" not in overlay:
        raise RuntimeError(f"OpenCode Desktop cannot route managed model {model}.")

    path = OPENCODE_DESKTOP_CONFIG_PATH
    backup_desktop_config("opencode")
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Cannot update OpenCode Desktop settings at {path}: {exc}") from exc
        if not isinstance(existing, dict):
            raise RuntimeError(f"OpenCode Desktop settings at {path} must be a JSON object.")
    else:
        existing = {}
    providers = existing.get("provider")
    if isinstance(providers, dict):
        for name in (
            "databricks-anthropic",
            "databricks-google",
            "databricks-openai",
            "databricks-oss",
        ):
            providers.pop(name, None)
    merged = deep_merge_dict(existing, overlay)
    write_text_file(
        _desktop_plugin_path(),
        opencode.render_auth_plugin(state),
    )
    write_json_file(path, merged)


def sync_desktop_config(tool: str, state: dict) -> None:
    if tool == "codex":
        sync_codex_desktop(state)
    elif tool == "opencode":
        sync_opencode_desktop(state)
