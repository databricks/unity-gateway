"""Launch-scoped control of smart subagent routing."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import MutableMapping
from pathlib import Path

from ucode.config_io import atomic_write_json

SESSION_STATE_ENV_VAR = "UCODE_SMART_ROUTER_SESSION_STATE"
SESSION_STATE_VERSION = 1


def start_session(env: MutableMapping[str, str] | None = None) -> Path:
    """Create a fresh enabled state file and expose it to the launched agent."""
    target = os.environ if env is None else env
    session_dir = Path(tempfile.mkdtemp(prefix="ug-smart-router-"))
    path = session_dir / "state.json"
    atomic_write_json(path, {"version": SESSION_STATE_VERSION, "enabled": True})
    target[SESSION_STATE_ENV_VAR] = str(path)
    return path


def clear_session(env: MutableMapping[str, str] | None = None) -> None:
    """Remove the launch-scoped pointer when a routed launch falls back."""
    target = os.environ if env is None else env
    target.pop(SESSION_STATE_ENV_VAR, None)


def session_state_path(env: MutableMapping[str, str] | None = None) -> Path:
    """Return this routed session's state path or explain how to start one."""
    source = os.environ if env is None else env
    value = source.get(SESSION_STATE_ENV_VAR, "").strip()
    if not value:
        raise RuntimeError(
            "Smart Router controls are available only inside a smart-routed Claude or Codex "
            "session. Relaunch with `ug claude --enable-smart-routing` or "
            "`ug codex --enable-smart-routing`."
        )
    return Path(value)


def _read_state(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict)
        or data.get("version") != SESSION_STATE_VERSION
        or not isinstance(data.get("enabled"), bool)
    ):
        raise ValueError("unrecognized state")
    return data


def routing_enabled(
    env: MutableMapping[str, str] | None = None,
    *,
    diagnostic: bool = True,
) -> bool:
    """Read the session switch, defaulting safely to enabled on any bad state."""
    try:
        path = session_state_path(env)
    except RuntimeError:
        # Old routing hooks and explicitly-invoked helpers can run without a
        # launch-scoped file. Preserve their historical enabled behavior.
        return True
    try:
        data = _read_state(path)
        enabled = data["enabled"]
        assert isinstance(enabled, bool)
        return enabled
    except (OSError, UnicodeError, ValueError) as exc:
        if diagnostic:
            import sys

            print(
                f"Smart Router could not read session state at {path} ({exc}); "
                "routing remains enabled.",
                file=sys.stderr,
            )
        return True


def set_routing_enabled(enabled: bool, env: MutableMapping[str, str] | None = None) -> Path:
    """Atomically set the current session switch."""
    path = session_state_path(env)
    try:
        data = _read_state(path)
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError(f"Smart Router session state at {path} is invalid.") from exc
    data["enabled"] = enabled
    atomic_write_json(path, data)
    return path
