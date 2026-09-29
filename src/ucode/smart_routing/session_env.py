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
    target = os.environ if env is None else env
    path = Path(tempfile.mkdtemp(prefix="ug-session-env-")) / "env.json"
    atomic_write_json(path, {})
    target[SESSION_ENV_VAR] = str(path)
    return path


def clear_session(env: MutableMapping[str, str] | None = None) -> None:
    (os.environ if env is None else env).pop(SESSION_ENV_VAR, None)


def session_env_path(env: Mapping[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    value = source.get(SESSION_ENV_VAR, "").strip()
    if value:
        return Path(value)
    raise RuntimeError(
        "Smart Router controls are available only inside a smart-routed Claude or Codex "
        "session. Relaunch with `ug claude --enable-smart-routing` or "
        "`ug codex --enable-smart-routing`."
    )


def _validate(values: object) -> dict[str, str]:
    if not isinstance(values, dict) or any(
        key not in _ALLOWED_KEYS or not isinstance(value, str) for key, value in values.items()
    ):
        raise ValueError("invalid session environment")
    return values


def _read(path: Path) -> dict[str, str]:
    return _validate(json.loads(path.read_text(encoding="utf-8")))


def effective_environment(env: Mapping[str, str] | None = None) -> dict[str, str]:
    effective = dict(os.environ if env is None else env)
    try:
        path = session_env_path(effective)
    except RuntimeError:
        return effective
    try:
        effective.update(_read(path))
    except (OSError, UnicodeError, ValueError) as exc:
        print(
            f"Smart Router could not read session environment at {path} ({exc}); "
            "using the inherited environment.",
            file=sys.stderr,
        )
    return effective


def set_session_environment(
    values: Mapping[str, str],
    env: Mapping[str, str] | None = None,
) -> Path:
    """Atomically replace the current session overrides."""
    path = session_env_path(env)
    try:
        _read(path)
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError(f"Smart Router session environment at {path} is invalid.") from exc
    atomic_write_json(path, _validate(dict(values)))
    return path
