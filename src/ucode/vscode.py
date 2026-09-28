"""Point the Claude Code VS Code extension at Unity Gateway.

The extension launches Claude from VS Code's own settings rather than through ``ug claude``,
so it never sees ``~/.claude/ucode-settings.json`` and falls back to Anthropic's sign-in. It
can, however, launch Claude through a ``claudeCode.claudeProcessWrapper`` executable. ug
installs ``ug-claude-vscode`` for that (:mod:`ucode.vscode_wrapper`); ``ug configure`` points
every VS Code that has the extension at it, and ``ug revert`` removes only what ug added.

Only strict-JSON ``settings.json`` files are edited: rewriting one that has comments or
trailing commas would drop them, so those get the settings printed for manual entry.

The Codex extension needs no VS Code settings (it reads Codex's own config files); ug only asks
:func:`has_codex_extension` whether to point those files at the gateway (see ``agents/codex.py``).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ucode import config_io
from ucode.ui import print_note, print_success, print_warning

CLAUDE_EXTENSION_ID = "anthropic.claude-code"
CODEX_EXTENSION_ID = "openai.chatgpt"
WRAPPER_COMMAND = "ug-claude-vscode"
WRAPPER_SETTING = "claudeCode.claudeProcessWrapper"
DISABLE_LOGIN_SETTING = "claudeCode.disableLoginPrompt"
PERMISSION_MODE_SETTING = "claudeCode.initialPermissionMode"
# With a wrapper the extension skips its usual permission-mode defaults and starts in Manual;
# setting Manual (`default`) explicitly keeps that predictable. A user's own choice wins.
DEFAULT_PERMISSION_MODE = "default"
_RECORD_NAME = "vscode-claude-extension.json"


@dataclass(frozen=True)
class VSCodeInstall:
    """One VS Code flavor on this machine.

    ``settings_path`` is the default profile's settings.json. ``user_dir`` is the ``User``
    folder that holds the other profiles (``profiles/<id>/``); None for the VS Code Server,
    which only has Machine settings.
    """

    name: str
    extensions_dir: Path
    settings_path: Path
    user_dir: Path | None = None


@dataclass(frozen=True)
class SettingsTarget:
    """A settings.json whose VS Code profile has the extension being looked for."""

    name: str
    settings_path: Path


def vscode_installs() -> list[VSCodeInstall]:
    """VS Code, VS Code Insiders, and the VS Code Server (Remote-SSH / WSL / containers)."""
    home = Path.home()
    if sys.platform == "win32":
        appdata = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")

        def user_dir(product: str) -> Path:
            return appdata / product / "User"

    elif sys.platform == "darwin":

        def user_dir(product: str) -> Path:
            return home / "Library" / "Application Support" / product / "User"

    else:
        config_home = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")

        def user_dir(product: str) -> Path:
            return config_home / product / "User"

    return [
        VSCodeInstall(
            "VS Code",
            home / ".vscode" / "extensions",
            user_dir("Code") / "settings.json",
            user_dir("Code"),
        ),
        VSCodeInstall(
            "VS Code Insiders",
            home / ".vscode-insiders" / "extensions",
            user_dir("Code - Insiders") / "settings.json",
            user_dir("Code - Insiders"),
        ),
        # When VS Code connects to this machine remotely, the extension runs here and reads the
        # server's Machine-scope settings.
        VSCodeInstall(
            "VS Code Server",
            home / ".vscode-server" / "extensions",
            home / ".vscode-server" / "data" / "Machine" / "settings.json",
        ),
    ]


def has_claude_extension(install: VSCodeInstall) -> bool:
    """Whether a Claude Code extension folder exists in ``install`` (any version/platform)."""
    return _has_extension_folder(install, CLAUDE_EXTENSION_ID)


def _has_extension_folder(install: VSCodeInstall, extension_id: str) -> bool:
    prefix = f"{extension_id}-"
    try:
        return any(
            entry.is_dir() and entry.name.lower().startswith(prefix)
            for entry in install.extensions_dir.iterdir()
        )
    except OSError:
        return False


def _lists_extension(extensions_json: Path, extension_id: str) -> bool | None:
    """Whether a profile's ``extensions.json`` lists the extension; None when it can't be read."""
    try:
        entries = json.loads(extensions_json.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(entries, list):
        return None
    return any(
        isinstance(entry, dict)
        and isinstance(entry.get("identifier"), dict)
        and str(entry["identifier"].get("id", "")).lower() == extension_id
        for entry in entries
    )


def _profiles(user_dir: Path) -> list[dict]:
    """VS Code's named profiles (``userDataProfiles`` in ``globalStorage/storage.json``)."""
    try:
        storage = json.loads(
            (user_dir / "globalStorage" / "storage.json").read_text(encoding="utf-8-sig")
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    profiles = storage.get("userDataProfiles") if isinstance(storage, dict) else None
    return [p for p in profiles if isinstance(p, dict)] if isinstance(profiles, list) else []


def claude_settings_targets(install: VSCodeInstall) -> list[SettingsTarget]:
    """The settings.json of every profile in ``install`` that has the Claude Code extension."""
    return _extension_settings_targets(install, CLAUDE_EXTENSION_ID)


def has_codex_extension(installs: Callable[[], list[VSCodeInstall]] | None = None) -> bool:
    """Whether any VS Code profile on this machine (or the VS Code Server) has the Codex extension."""
    return any(
        _extension_settings_targets(install, CODEX_EXTENSION_ID)
        for install in (installs or vscode_installs)()
    )


def _extension_settings_targets(install: VSCodeInstall, extension_id: str) -> list[SettingsTarget]:
    """The settings.json of every profile in ``install`` that has the extension.

    The default profile's extensions are listed in ``<extensions dir>/extensions.json``; each
    other profile has its own ``extensions.json`` and ``settings.json`` under
    ``User/profiles/<id>/`` unless its ``useDefaultFlags`` shares the default profile's.
    """
    listed = _lists_extension(install.extensions_dir / "extensions.json", extension_id)
    default_has = _has_extension_folder(install, extension_id) if listed is None else listed
    targets: list[SettingsTarget] = []
    if default_has:
        targets.append(SettingsTarget(install.name, install.settings_path))
    if install.user_dir is not None:
        for profile in _profiles(install.user_dir):
            location = profile.get("location")
            if not isinstance(location, str) or not location:
                continue
            flags = profile.get("useDefaultFlags")
            flags = flags if isinstance(flags, dict) else {}
            profile_dir = install.user_dir / "profiles" / Path(location)
            has = (
                default_has
                if flags.get("extensions")
                else _lists_extension(profile_dir / "extensions.json", extension_id) is True
            )
            if not has:
                continue
            settings = (
                install.settings_path if flags.get("settings") else profile_dir / "settings.json"
            )
            targets.append(
                SettingsTarget(
                    f"{install.name} (profile {profile.get('name') or location})", settings
                )
            )
    unique: dict[Path, SettingsTarget] = {}
    for target in targets:
        unique.setdefault(target.settings_path, target)
    return list(unique.values())


def wrapper_executable() -> str | None:
    """The installed ``ug-claude-vscode`` executable, if any."""
    found = shutil.which(WRAPPER_COMMAND)
    if found:
        return found
    # Installed next to `ug` by the same package even when that directory isn't on PATH.
    if sys.argv and sys.argv[0]:
        suffix = ".exe" if os.name == "nt" else ""
        sibling = Path(sys.argv[0]).absolute().with_name(WRAPPER_COMMAND + suffix)
        if sibling.is_file():
            return str(sibling)
    return None


def _is_ug_wrapper(value: object) -> bool:
    """A wrapper setting ug wrote, possibly from another install location."""
    if not isinstance(value, str) or not value.strip():
        return False
    return Path(value.strip()).stem.lower() == WRAPPER_COMMAND


def _record_path() -> Path:
    return config_io.APP_DIR / _RECORD_NAME


def _read_record() -> dict[str, dict]:
    try:
        data = json.loads(_record_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_record(record: dict[str, dict]) -> None:
    path = _record_path()
    if not record:
        path.unlink(missing_ok=True)
        return
    config_io.ensure_parent_dir(path)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def _read_settings(path: Path) -> dict | None:
    """``settings.json`` as a dict ({} when absent), or None when it isn't strict JSON."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError):
        return None
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _desired_settings(doc: dict, wrapper: str) -> dict[str, object]:
    desired: dict[str, object] = {WRAPPER_SETTING: wrapper, DISABLE_LOGIN_SETTING: True}
    if PERMISSION_MODE_SETTING not in doc:
        desired[PERMISSION_MODE_SETTING] = DEFAULT_PERMISSION_MODE
    return desired


def _manual_lines(wrapper: str) -> str:
    return "\n".join(
        f"    {json.dumps(key)}: {json.dumps(value)},"
        for key, value in (
            (WRAPPER_SETTING, wrapper),
            (DISABLE_LOGIN_SETTING, True),
            (PERMISSION_MODE_SETTING, DEFAULT_PERMISSION_MODE),
        )
    )


def _configure_install(install: SettingsTarget, wrapper: str, record: dict[str, dict]) -> None:
    path = install.settings_path
    doc = _read_settings(path)
    if doc is None:
        print_warning(
            f"{install.name}: {path} has comments or isn't plain JSON, so ug left it untouched. "
            "To use Unity Gateway in the Claude Code extension, add these settings yourself:\n"
            + _manual_lines(wrapper)
        )
        return
    current = doc.get(WRAPPER_SETTING)
    if current and not _is_ug_wrapper(current):
        print_warning(
            f"{install.name}: the Claude Code extension already launches through {current}; "
            f"ug left it alone. To use Unity Gateway instead, set {WRAPPER_SETTING} to {wrapper} "
            f"in {path}."
        )
        return
    changes = {
        key: value
        for key, value in _desired_settings(doc, wrapper).items()
        if doc.get(key) != value
    }
    if not changes:
        print_note(f"{install.name}: the Claude Code extension already uses Unity Gateway.")
        return
    entry = record.setdefault(str(path), {"set": {}, "previous": {}})
    for key, value in changes.items():
        if key in doc and key not in entry["previous"] and key not in entry["set"]:
            entry["previous"][key] = doc[key]
        doc[key] = value
        entry["set"][key] = value
    config_io.write_text_file(path, json.dumps(doc, indent=4, ensure_ascii=False) + "\n")
    if not config_io.is_dry_run():
        print_success(
            f"{install.name}: Claude Code extension set to use Unity Gateway ({path}). "
            "Reload VS Code to apply."
        )


def configure_claude_extension(
    installs: Callable[[], list[VSCodeInstall]] | None = None,
) -> None:
    """Point every VS Code profile with the Claude Code extension at ug's wrapper.

    Silent when no VS Code on this machine has the extension.
    """
    targets = [
        target
        for install in (installs or vscode_installs)()
        for target in claude_settings_targets(install)
    ]
    if not targets:
        return
    wrapper = wrapper_executable()
    if wrapper is None:
        print_warning(
            f"The Claude Code VS Code extension is installed, but `{WRAPPER_COMMAND}` was not "
            "found; reinstall Unity Gateway to let the extension use it."
        )
        return
    record = _read_record()
    for install in targets:
        _configure_install(install, wrapper, record)
    if not config_io.is_dry_run():
        _write_record(record)


def revert_claude_extension() -> str:
    """Undo the settings ug added, leaving any a user has since changed. Returns a summary."""
    record = _read_record()
    reverted = False
    for settings_path, entry in list(record.items()):
        path = Path(settings_path)
        doc = _read_settings(path)
        if doc is None or not path.exists():
            continue
        changed = False
        for key, value in (entry.get("set") or {}).items():
            if doc.get(key) != value:
                continue  # changed since ug set it: the user's value stays
            previous = entry.get("previous") or {}
            if key in previous:
                doc[key] = previous[key]
            else:
                doc.pop(key, None)
            changed = True
        if changed:
            config_io.write_text_file(path, json.dumps(doc, indent=4, ensure_ascii=False) + "\n")
            reverted = True
    if not config_io.is_dry_run():
        _write_record({})
    return "restored" if reverted else "unchanged"
