"""Session-scoped environment overrides read by smart-routing hooks."""

from __future__ import annotations

import errno
import json
import logging
import os
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path

from ucode.config_io import atomic_write_json
from ucode.constants import (
    ENABLE_SMART_ROUTING_ENV_VAR,
    ENABLE_SUBAGENT_ROUTING_ENV_VAR,
    SMART_ROUTING_ENV_KEYS,
)
from ucode.smart_routing.config import resolve_environment

SESSION_ENV_VAR = "UCODE_SESSION_ENV_FILE"
SESSION_PYTHON_ENV_VAR = "UCODE_SMART_ROUTER_PYTHON"
CONTROLS_READ_ATTEMPTS = 10
CONTROLS_READ_RETRY_SECONDS = 0.1
ON_DEMAND_FILE_NAME = "smart-routing-on-demand.json"
_ALLOWED_KEYS = frozenset(SMART_ROUTING_ENV_KEYS)


@dataclass(frozen=True)
class OnDemandRouting:
    """What a session that started with Smart Router off records about turning it on."""

    when_on: dict[str, str]  # The routing `on` applies.
    ever_on: bool  # Whether the user has turned it on yet.


def start_session(env: MutableMapping[str, str] | None = None) -> Path:
    """Create an empty override file and expose it to the launched harness."""
    target = os.environ if env is None else env
    path = Path(tempfile.mkdtemp(prefix="ug-session-env-")) / "env.json"
    atomic_write_json(path, {})
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


def _validate(values: object) -> dict[str, str]:
    if not isinstance(values, dict) or any(
        key not in _ALLOWED_KEYS or not isinstance(value, str) for key, value in values.items()
    ):
        raise ValueError("invalid session environment")
    return values


def _read(path: Path) -> dict[str, str]:
    return _validate(json.loads(path.read_text(encoding="utf-8")))


def _retry[T](read: Callable[[], T]) -> T:
    attempts_remaining = CONTROLS_READ_ATTEMPTS
    while True:
        try:
            return read()
        except PermissionError:
            attempts_remaining -= 1
            if not attempts_remaining:
                raise
            time.sleep(CONTROLS_READ_RETRY_SECONDS)


def _read_with_retry(path: Path) -> dict[str, str]:
    return _retry(lambda: _read(path))


def on_demand_path(controls: Path) -> Path:
    return controls.with_name(ON_DEMAND_FILE_NAME)


def start_on_demand(controls: Path, when_on: Mapping[str, str]) -> Path:
    """Switch a new session's Smart Router off until the user turns it on."""
    path = on_demand_path(controls)
    atomic_write_json(path, {"when_on": _validate(dict(when_on)), "ever_on": False})
    atomic_write_json(controls, dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0"))
    return path


def _read_on_demand(path: Path) -> OnDemandRouting:
    values = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(values, dict) or not isinstance(values.get("ever_on"), bool):
        raise ValueError("invalid on-demand routing")
    return OnDemandRouting(when_on=_validate(values.get("when_on")), ever_on=values["ever_on"])


def routing_never_turned_on(env: Mapping[str, str] | None = None) -> bool:
    """Whether this session started with Smart Router off and the user hasn't turned it on."""
    try:
        path = on_demand_path(session_env_path(env))
        return path.exists() and not _retry(lambda: _read_on_demand(path)).ever_on
    except (RuntimeError, OSError, UnicodeError, ValueError):
        return False


def session_routing_enabled(path: Path, *, retry: bool = True) -> bool:
    overrides = _read_with_retry(path) if retry else _read(path)
    return not all(
        overrides.get(key) == "0"
        for key in (ENABLE_SMART_ROUTING_ENV_VAR, ENABLE_SUBAGENT_ROUTING_ENV_VAR)
    )


def effective_environment(
    env: Mapping[str, str] | None = None, *, agent: str, strict: bool = False
) -> dict[str, str]:
    """Overlay the latest session controls on the hook process environment."""
    effective = resolve_environment(env, agent=agent)
    try:
        path = session_env_path(effective)
    except RuntimeError:
        return effective
    try:
        effective.update(_read_with_retry(path))
    except (OSError, UnicodeError, ValueError) as exc:
        logging.getLogger(__name__).debug(
            "Smart Router could not read session controls at %s: %s", path, exc
        )
        if strict:
            raise
    return effective


def _permission_help(env: Mapping[str, str] | None) -> str:
    return (
        "Request native command approval to retry this command."
        if env is None
        else "Check that the launcher can access this session's controls."
    )


def _read_for_update[T](path: Path, read: Callable[[], T], env: Mapping[str, str] | None) -> T:
    try:
        return _retry(read)
    except PermissionError as exc:
        raise RuntimeError(
            f"Permission denied reading Smart Router session controls at {path}. "
            f"{_permission_help(env)}"
        ) from exc
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError(f"Smart Router session controls at {path} are invalid.") from exc


def _write(path: Path, values: object, env: Mapping[str, str] | None) -> None:
    try:
        atomic_write_json(path, values)
    except (OSError, RuntimeError) as exc:
        # The atomic writer wraps mkdir and replacement failures, but not mkstemp failures.
        cause = exc.__cause__ if isinstance(exc, RuntimeError) else exc
        if isinstance(cause, PermissionError) or (
            isinstance(cause, OSError) and cause.errno in (errno.EACCES, errno.EPERM, errno.EROFS)
        ):
            raise RuntimeError(
                f"Permission denied writing Smart Router session controls at {path}. "
                f"{_permission_help(env)}"
            ) from exc
        raise RuntimeError(
            f"Error writing Smart Router session controls at {path}: {cause or exc}"
        ) from exc


def set_session_environment(
    values: Mapping[str, str], *, env: Mapping[str, str] | None = None
) -> bool:
    """Atomically replace changed session overrides."""
    path = session_env_path(env)
    current = _read_for_update(path, lambda: _read(path), env)
    requested = _validate(dict(values))
    if current == requested:
        return False
    _write(path, requested, env)
    return True


def set_session_routing(enabled: bool, *, env: Mapping[str, str] | None = None) -> bool:
    """Turn this session's Smart Router on or off and return whether its controls changed.

    `on` restores the routing the session launched with. A session that started with Smart
    Router off keeps that routing in its on-demand file instead.
    """
    if not enabled:
        return set_session_environment(dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0"), env=env)
    path = on_demand_path(session_env_path(env))
    if not path.exists():
        return set_session_environment({}, env=env)
    on_demand = _read_for_update(path, lambda: _read_on_demand(path), env)
    if not on_demand.ever_on:
        _write(path, {"when_on": on_demand.when_on, "ever_on": True}, env)
    return set_session_environment(on_demand.when_on, env=env)
