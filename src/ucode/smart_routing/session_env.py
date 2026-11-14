"""Session-scoped environment overrides read by smart-routing hooks."""

from __future__ import annotations

import errno
import json
import logging
import os
import sys
import tempfile
import time
from collections.abc import Mapping, MutableMapping
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
_ALLOWED_KEYS = frozenset(SMART_ROUTING_ENV_KEYS)


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


def _read_with_retry(path: Path) -> dict[str, str]:
    attempts_remaining = CONTROLS_READ_ATTEMPTS
    while True:
        try:
            return _read(path)
        except PermissionError:
            attempts_remaining -= 1
            if not attempts_remaining:
                raise
            time.sleep(CONTROLS_READ_RETRY_SECONDS)


def session_routing_enabled(path: Path) -> bool:
    overrides = _read_with_retry(path)
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


def set_session_environment(values: Mapping[str, str]) -> bool:
    """Atomically replace changed session overrides."""
    path = session_env_path()
    try:
        current = _read_with_retry(path)
    except PermissionError as exc:
        raise RuntimeError(
            f"Permission denied reading Smart Router session controls at {path}. "
            "Request native command approval to retry this command."
        ) from exc
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError(f"Smart Router session controls at {path} are invalid.") from exc
    requested = _validate(dict(values))
    if current == requested:
        return False
    try:
        atomic_write_json(path, requested)
    except (OSError, RuntimeError) as exc:
        # The atomic writer wraps mkdir and replacement failures, but not mkstemp failures.
        cause = exc.__cause__ if isinstance(exc, RuntimeError) else exc
        if isinstance(cause, PermissionError) or (
            isinstance(cause, OSError) and cause.errno in (errno.EACCES, errno.EPERM, errno.EROFS)
        ):
            raise RuntimeError(
                f"Permission denied writing Smart Router session controls at {path}. "
                "Request native command approval to retry this command."
            ) from exc
        raise RuntimeError(
            f"Error writing Smart Router session controls at {path}: {cause or exc}"
        ) from exc
    return True
