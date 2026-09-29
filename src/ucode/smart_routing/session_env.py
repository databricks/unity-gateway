"""Launch-scoped environment overrides reloaded by smart-routing hooks."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Mapping, MutableMapping
from pathlib import Path

from ucode.config_io import atomic_write_json
from ucode.constants import SMART_ROUTING_ENV_KEYS

SESSION_ENV_VAR = "UCODE_SESSION_ENV_FILE"
_ALLOWED_KEYS = frozenset(SMART_ROUTING_ENV_KEYS)


def start_session(env: MutableMapping[str, str] | None = None) -> Path:
    """Create an empty per-launch override file and expose it to child hooks."""
    target = os.environ if env is None else env
    session_dir = Path(tempfile.mkdtemp(prefix="ug-session-env-"))
    path = session_dir / "env.json"
    atomic_write_json(path, {})
    target[SESSION_ENV_VAR] = str(path)
    return path


def clear_session(env: MutableMapping[str, str] | None = None) -> None:
    target = os.environ if env is None else env
    target.pop(SESSION_ENV_VAR, None)


def session_env_path(env: Mapping[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    value = source.get(SESSION_ENV_VAR, "").strip()
    if not value:
        raise RuntimeError(
            "Smart Router controls are available only inside a smart-routed Claude or Codex "
            "session. Relaunch with `ug claude --enable-smart-routing` or "
            "`ug codex --enable-smart-routing`."
        )
    return Path(value)


def _validate(values: object, *, allow_none: bool = False) -> dict[str, str | None]:
    if not isinstance(values, dict):
        raise ValueError("session environment must be an object")
    validated: dict[str, str | None] = {}
    for key, value in values.items():
        if key not in _ALLOWED_KEYS:
            raise ValueError(f"unsupported session environment key: {key!r}")
        if not isinstance(value, str) and not (allow_none and value is None):
            raise ValueError(f"invalid session environment value for {key!r}")
        validated[key] = value
    return validated


def _read(path: Path) -> dict[str, str]:
    values = _validate(json.loads(path.read_text(encoding="utf-8")))
    return {key: value for key, value in values.items() if isinstance(value, str)}


def effective_environment(
    env: Mapping[str, str] | None = None,
    *,
    diagnostic: bool = True,
) -> dict[str, str]:
    """Merge the current hook environment with its latest session overrides."""
    effective = dict(os.environ if env is None else env)
    try:
        path = session_env_path(effective)
    except RuntimeError:
        return effective
    try:
        effective.update(_read(path))
    except (OSError, UnicodeError, ValueError) as exc:
        if diagnostic:
            print(
                f"Smart Router could not read session environment at {path} ({exc}); "
                "using the inherited environment.",
                file=sys.stderr,
            )
    return effective


def set_session_environment(
    values: Mapping[str, str | None],
    env: Mapping[str, str] | None = None,
) -> Path:
    """Atomically update allowlisted overrides; ``None`` restores inheritance."""
    path = session_env_path(env)
    requested = _validate(dict(values), allow_none=True)
    try:
        current = _read(path)
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError(f"Smart Router session environment at {path} is invalid.") from exc
    for key, value in requested.items():
        if value is None:
            current.pop(key, None)
        else:
            current[key] = value
    atomic_write_json(path, current)
    return path
