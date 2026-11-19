"""Turns the GitHub Copilot app's own span export on and off.

The app's runtime writes its spans to ``copilot_otel_file.SPAN_FILE`` once it sees two
Copilot-only variables, and ug's login service forwards them. The variables reach the app through
a block in the login-shell profile only the app's environment probe runs (macOS, Linux), or the
user's environment (Windows, where the app runs no probe).
"""

from __future__ import annotations

from pathlib import Path

from ucode import copilot_otel_file, shell_profile_env
from ucode.config_io import APP_DIR, atomic_write_json, read_json_safe
from ucode.managed_files import OS, current_os
from ucode.os_compatibility import windows_user_env
from ucode.ui import print_warning

# The profile files ug wrote to, and the ones it created, so revert touches exactly those.
RECORD = APP_DIR / "copilot-app" / "tracing-profiles.json"


def app_env() -> dict[str, str]:
    return {
        "COPILOT_OTEL_ENABLED": "true",
        "COPILOT_OTEL_FILE_EXPORTER_PATH": str(copilot_otel_file.SPAN_FILE),
    }


def _record() -> tuple[list[Path], list[Path]]:
    record = read_json_safe(RECORD)

    def paths(key: str) -> list[Path]:
        values = record.get(key)
        return (
            [Path(value) for value in values if isinstance(value, str)]
            if isinstance(values, list)
            else []
        )

    return paths("paths"), paths("created")


def enable() -> bool:
    """Export the variables to the app. Returns whether the app needs a restart to pick them up."""
    env = app_env()
    if current_os() == OS.WINDOWS:
        changed, kept = windows_user_env.set_values(env)
        if kept:
            print_warning(
                f"GitHub Copilot app tracing: left your own {', '.join(kept)} in place; the app's "
                "traces may not reach the workspace."
            )
        return changed
    shells = shell_profile_env.login_shells()
    paths = list(
        dict.fromkeys(
            path for shell in shells if (path := shell_profile_env.profile_file(shell)) is not None
        )
    )
    if not paths:
        print_warning(
            "GitHub Copilot app tracing: the app doesn't read the profile of your login shell "
            f"({', '.join(shells) or 'none set'}), so its traces won't be exported. Supported: "
            f"{', '.join(shell_profile_env.SUPPORTED_SHELLS)}."
        )
        return False
    recorded, created = _record()
    created = list(dict.fromkeys([*created, *(path for path in paths if not path.exists())]))
    # Recorded before writing, so a write that fails partway still leaves revert every block.
    _save_record(list(dict.fromkeys([*recorded, *paths])), created)
    changed = False
    for path in paths:
        changed = shell_profile_env.write(path, env) or changed
    for stale in recorded:
        if stale not in paths:
            _remove_profile(stale, created)
    _save_record(paths, created)
    return changed


def _save_record(paths: list[Path], created: list[Path]) -> None:
    atomic_write_json(
        RECORD,
        {
            "paths": [str(path) for path in paths],
            "created": [str(path) for path in created if path in paths],
        },
    )


def _remove_profile(path: Path, created: list[Path]) -> bool:
    changed = shell_profile_env.remove(path)
    # A profile ug created only for the block goes with it.
    if path in created and path.is_file() and not path.read_text(encoding="utf-8").strip():
        path.unlink()
    return changed


def disable() -> bool:
    """Stop exporting the variables to the app. Returns whether anything was removed."""
    if current_os() == OS.WINDOWS:
        return windows_user_env.remove_values(app_env())
    recorded, created = _record()
    changed = False
    for path in recorded:
        changed = _remove_profile(path, created) or changed
    RECORD.unlink(missing_ok=True)
    return changed
