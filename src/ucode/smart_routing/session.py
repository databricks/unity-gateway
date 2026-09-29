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
SESSION_AGENTS = frozenset({"claude", "codex"})


def start_session(
    env: MutableMapping[str, str] | None = None,
    *,
    agent: str | None = None,
) -> Path:
    """Create a fresh enabled state file and expose it to the launched agent.

    ``agent`` identifies the launcher that owns the session.  It is optional for
    compatibility with state files created by older ug versions; new routed
    launches set it so the session-local launcher flags cannot accidentally
    toggle a different harness.
    """
    target = os.environ if env is None else env
    session_dir = Path(tempfile.mkdtemp(prefix="ug-smart-router-"))
    path = session_dir / "state.json"
    data: dict[str, object] = {"version": SESSION_STATE_VERSION, "enabled": True}
    if agent in SESSION_AGENTS:
        data["agent"] = agent
    atomic_write_json(path, data)
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


def session_agent(env: MutableMapping[str, str] | None = None) -> str | None:
    """Return the owning launcher for the current session, if it is valid."""
    try:
        path = session_state_path(env)
        data = _read_state(path)
    except (RuntimeError, OSError, UnicodeError, ValueError):
        return None
    agent = data.get("agent")
    return agent if isinstance(agent, str) and agent in SESSION_AGENTS else None


def session_state_valid(env: MutableMapping[str, str] | None = None) -> bool:
    """Whether the session pointer names a readable, recognized state file."""
    try:
        path = session_state_path(env)
        _read_state(path)
    except (RuntimeError, OSError, UnicodeError, ValueError):
        return False
    return True


def is_active_session(
    agent: str | None = None,
    env: MutableMapping[str, str] | None = None,
) -> bool:
    """Whether a valid routed-session state exists and optionally belongs to ``agent``."""
    try:
        path = session_state_path(env)
        data = _read_state(path)
    except (RuntimeError, OSError, UnicodeError, ValueError):
        return False
    if agent is None:
        return True
    # State files from the first session-control release had no owner field.
    # Treat those as belonging to the current routed process for compatibility;
    # all newly created sessions carry an explicit owner.
    owner = data.get("agent")
    return owner is None or owner == agent


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
