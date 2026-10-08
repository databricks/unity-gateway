"""Session-scoped environment overrides read by smart-routing hooks."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, MutableMapping
from pathlib import Path

from ucode.constants import (
    CLAUDE_CODE_EXTRA_BODY,
    SMART_ROUTER_RECIPE_LOCAL,
    SMART_ROUTING_ENV_KEYS,
)
from ucode.session_settings import (
    create_session_settings,
    read_session_settings,
    replace_session_settings,
)

SESSION_ENV_VAR = "UCODE_SESSION_ENV_FILE"
SESSION_PYTHON_ENV_VAR = "UCODE_SMART_ROUTER_PYTHON"
_ALLOWED_KEYS = frozenset(
    (*SMART_ROUTING_ENV_KEYS, SMART_ROUTER_RECIPE_LOCAL, CLAUDE_CODE_EXTRA_BODY)
)


def start_session(env: MutableMapping[str, str] | None = None) -> Path:
    """Create an empty override file and expose it to the launched harness."""
    target = os.environ if env is None else env
    target.pop(SMART_ROUTER_RECIPE_LOCAL, None)
    path = create_session_settings(filename="env.json")
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


def read_session_environment(path: Path) -> dict[str, str]:
    """Read one session's routing controls through the shared settings store."""
    return _validate(read_session_settings(path))


def effective_environment(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Overlay the latest session controls on the hook process environment."""
    effective = dict(os.environ if env is None else env)
    try:
        path = session_env_path(effective)
    except RuntimeError:
        return effective
    try:
        effective.update(read_session_environment(path))
    except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
        print(
            f"Smart Router could not read session controls at {path} ({exc}); "
            "using the inherited environment.",
            file=sys.stderr,
        )
    return effective


def set_session_environment(values: Mapping[str, str], *, path: Path | None = None) -> None:
    """Atomically replace the current session's allowlisted overrides."""
    path = path if path is not None else session_env_path()
    try:
        read_session_environment(path)
    except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
        raise RuntimeError(f"Smart Router session controls at {path} are invalid.") from exc
    replace_session_settings(path, _validate(dict(values)))
