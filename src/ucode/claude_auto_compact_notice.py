"""Claude Code ``UserPromptSubmit`` hook: tell the user when `/autocompact` needs a restart.

ug's ``--settings`` entry for a capped model outranks the user's ``~/.claude/settings.json`` and is not
hot-reloaded, so a mid-session `/autocompact` only applies on the next ``ug claude``. Run as
``python -m ucode.claude_auto_compact_notice`` so it imports no more than the standard library and
``ucode.auto_compact_window``. It reads the user file and never writes it.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from ucode.auto_compact_window import AUTO_COMPACT_CAP, valid_auto_compact_window

HOOK_COMMAND_MARKER = "ucode.claude_auto_compact_notice"
# Set on the Claude process at launch: {"<id without [1m]>": {"effective": int, "user": int|null}},
# where "user" is the valid value in the user's settings at launch.
SESSION_WINDOWS_ENV_VAR = "UCODE_AUTO_COMPACT_SESSION"
NOTICE_STATE_DIR_NAME = "auto-compact-notices"
_STALE_STATE_SECONDS = 7 * 24 * 3600
_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def user_auto_compact_windows(settings: object) -> dict[str, int]:
    """Valid ``autoCompactWindow`` values per id without `[1m]`; the lower spelling wins."""
    model_settings = settings.get("modelSettings") if isinstance(settings, dict) else None
    if not isinstance(model_settings, dict):
        return {}
    windows: dict[str, int] = {}
    for model, entry in model_settings.items():
        if not isinstance(model, str) or not isinstance(entry, dict):
            continue
        window = valid_auto_compact_window(entry.get("autoCompactWindow"))
        if window is not None:
            canonical = model.removesuffix("[1m]")
            windows[canonical] = min(windows.get(canonical, window), window)
    return windows


def notice_for(model: str, effective: int, launch_user: int | None, user: int | None) -> str | None:
    """The restart notice for ``model``, or None when the user's file needs no message.

    Silent without a valid user value, when it equals the session's effective value, and when it is
    unchanged since launch (a standing over-cap value was already warned about by ug at launch).
    """
    if user is None or user == effective or user == launch_user:
        return None
    capped = min(user, AUTO_COMPACT_CAP)
    if user > AUTO_COMPACT_CAP:
        message = (
            f"Auto-compact for {model} stays at {capped // 1000}k: ug caps it because the "
            "gateway rejects longer requests for this model."
        )
        if capped != effective:
            message += " Restart ug claude to apply the cap."
        return message
    return f"Auto-compact for {model} changes to {user // 1000}k after you restart ug claude."


def _session_windows() -> dict[str, tuple[int, int | None]]:
    raw = json.loads(os.environ.get(SESSION_WINDOWS_ENV_VAR) or "{}")
    windows: dict[str, tuple[int, int | None]] = {}
    for model, entry in raw.items():
        effective = valid_auto_compact_window(entry.get("effective"))
        if effective is None:
            continue
        windows[model] = (effective, valid_auto_compact_window(entry.get("user")))
    return windows


def _state_path(session_id: str) -> Path | None:
    if not _SAFE_SESSION_ID.match(session_id):
        return None
    return Path.home() / ".ucode" / NOTICE_STATE_DIR_NAME / f"{session_id}.json"


def _prune_stale_state(state_dir: Path) -> None:
    cutoff = time.time() - _STALE_STATE_SECONDS
    for path in state_dir.glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue


def pending_notices(session_id: str, user_settings_path: Path | None = None) -> list[str]:
    """Notices not yet shown in this session; records them as shown."""
    windows = _session_windows()
    if not windows:
        return []
    path = user_settings_path or Path.home() / ".claude" / "settings.json"
    try:
        user_windows = user_auto_compact_windows(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return []
    state_path = _state_path(session_id)
    if state_path is None:
        return []
    try:
        shown = set(json.loads(state_path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        shown = set()
    messages: list[str] = []
    for model, (effective, launch_user) in windows.items():
        user = user_windows.get(model)
        message = notice_for(model, effective, launch_user, user)
        key = f"{model}={user}"
        if message is None or key in shown:
            continue
        shown.add(key)
        messages.append(message)
    if messages:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(sorted(shown)), encoding="utf-8")
        _prune_stale_state(state_path.parent)
    return messages


def main() -> int:
    """Print a ``systemMessage`` when a notice is due; always exit 0 so a prompt never fails."""
    try:
        if not os.environ.get(SESSION_WINDOWS_ENV_VAR):
            return 0
        payload = json.loads(sys.stdin.read() or "{}")
        session_id = payload.get("session_id") if isinstance(payload, dict) else None
        if not isinstance(session_id, str):
            return 0
        messages = pending_notices(session_id)
        if messages:
            sys.stdout.write(json.dumps({"systemMessage": "\n".join(messages)}))
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
