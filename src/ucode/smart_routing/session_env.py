"""Session-scoped Smart Router controls read by smart-routing hooks.

The session file is the session's switch: ``enabled`` says whether Smart Router is on now,
``when_on`` holds the routing flags that apply while it is on, and ``ever_enabled`` records
whether it has been on at any point. Files from earlier releases hold flat flag overrides
instead; they keep their original meaning.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Mapping, MutableMapping
from pathlib import Path

from ucode.config_io import atomic_write_json
from ucode.constants import SMART_ROUTING_ENV_KEYS
from ucode.smart_routing.config import resolve_environment

SESSION_ENV_VAR = "UCODE_SESSION_ENV_FILE"
SESSION_PYTHON_ENV_VAR = "UCODE_SMART_ROUTER_PYTHON"
_ALLOWED_KEYS = frozenset(SMART_ROUTING_ENV_KEYS)
_SWITCH_KEYS = frozenset({"enabled", "when_on", "ever_enabled"})
_ROUTING_OFF = dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")


def start_session(
    env: MutableMapping[str, str] | None = None,
    *,
    enabled: bool,
    when_on: Mapping[str, str],
) -> Path:
    """Create the session's switch and expose it to the launched harness."""
    target = os.environ if env is None else env
    path = Path(tempfile.mkdtemp(prefix="ug-session-env-")) / "env.json"
    atomic_write_json(path, {"enabled": enabled, "when_on": dict(when_on), "ever_enabled": enabled})
    target[SESSION_ENV_VAR] = str(path)
    # Preserve the virtualenv executable: resolving its symlink can select system Python.
    # The skill uses this interpreter with -m ucode.cli, independent of the tool's PATH.
    target[SESSION_PYTHON_ENV_VAR] = sys.executable
    return path


def session_env_path(env: Mapping[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    value = source.get(SESSION_ENV_VAR, "").strip()
    if value:
        return Path(value)
    raise RuntimeError(
        "Smart Router controls are available only inside a smart-routed Claude or Codex session."
    )


def _is_switch(controls: Mapping) -> bool:
    return controls.keys() == _SWITCH_KEYS


def _validate_overrides(values: object) -> dict[str, str]:
    if not isinstance(values, dict) or any(
        key not in _ALLOWED_KEYS or not isinstance(value, str) for key, value in values.items()
    ):
        raise ValueError("invalid session environment")
    return values


def _validate(controls: object) -> dict:
    if not isinstance(controls, dict) or not _is_switch(controls):
        return _validate_overrides(controls)
    if not isinstance(controls["enabled"], bool) or not isinstance(controls["ever_enabled"], bool):
        raise ValueError("invalid session environment")
    _validate_overrides(controls["when_on"])
    return controls


def _read(path: Path) -> dict:
    return _validate(json.loads(path.read_text(encoding="utf-8")))


def _overrides(controls: dict) -> dict[str, str]:
    if not _is_switch(controls):
        return controls
    return controls["when_on"] if controls["enabled"] else _ROUTING_OFF


def effective_environment(env: Mapping[str, str] | None = None, *, agent: str) -> dict[str, str]:
    """Overlay the latest session controls on the hook process environment."""
    effective = resolve_environment(env, agent=agent)
    try:
        path = session_env_path(effective)
    except RuntimeError:
        return effective
    try:
        effective.update(_overrides(_read(path)))
    except (OSError, UnicodeError, ValueError) as exc:
        print(
            f"Smart Router could not read session controls at {path} ({exc}); "
            "using the inherited environment.",
            file=sys.stderr,
        )
    return effective


def set_session_enabled(enabled: bool) -> bool:
    """Atomically turn the current session's Smart Router on or off.

    Returns whether this turned it on for the first time in the session.
    """
    path = session_env_path()
    try:
        controls = _read(path)
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError(f"Smart Router session controls at {path} are invalid.") from exc
    if not _is_switch(controls):
        atomic_write_json(path, {} if enabled else _ROUTING_OFF)
        return False
    atomic_write_json(
        path,
        {**controls, "enabled": enabled, "ever_enabled": controls["ever_enabled"] or enabled},
    )
    return enabled and not controls["ever_enabled"]
