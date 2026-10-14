"""Session-scoped environment overrides read by smart-routing hooks."""

from __future__ import annotations

import json
import os
import sys
import tempfile
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
_ALLOWED_KEYS = frozenset(SMART_ROUTING_ENV_KEYS)


def start_session(
    env: MutableMapping[str, str] | None = None, *, baseline: Mapping[str, str] | None = None
) -> Path:
    """Create an empty override file and expose it to the launched harness."""
    target = os.environ if env is None else env
    path = Path(tempfile.mkdtemp(prefix="ug-session-env-")) / "env.json"
    if baseline is not None:
        atomic_write_json(
            path.with_name("baseline.json"),
            {key: baseline.get(key, "0") for key in SMART_ROUTING_ENV_KEYS},
        )
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


def enabled_session_environment(*, agent: str) -> dict[str, str]:
    """Restore the launch mode, or enable subagent routing for an initially off session."""
    path = session_env_path().with_name("baseline.json")
    try:
        baseline = _read(path)
        if baseline.keys() != _ALLOWED_KEYS:
            raise ValueError("incomplete launch baseline")
    except FileNotFoundError:
        # Sessions launched by older UG versions have only the override file.
        source = resolve_environment(agent=agent)
        baseline = {key: source.get(key, "0") for key in SMART_ROUTING_ENV_KEYS}
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError(f"Smart Router launch baseline at {path} is invalid.") from exc
    if not any(
        baseline[key] == "1"
        for key in (ENABLE_SMART_ROUTING_ENV_VAR, ENABLE_SUBAGENT_ROUTING_ENV_VAR)
    ):
        baseline[ENABLE_SUBAGENT_ROUTING_ENV_VAR] = "1"
    return baseline


def effective_environment(env: Mapping[str, str] | None = None, *, agent: str) -> dict[str, str]:
    """Overlay the latest session controls on the hook process environment."""
    effective = resolve_environment(env, agent=agent)
    try:
        path = session_env_path(effective)
    except RuntimeError:
        return effective
    try:
        effective.update(_read(path))
    except (OSError, UnicodeError, ValueError) as exc:
        print(
            f"Smart Router could not read session controls at {path} ({exc}); "
            "using the inherited environment.",
            file=sys.stderr,
        )
    return effective


def set_session_environment(values: Mapping[str, str]) -> None:
    """Atomically replace the current session's allowlisted overrides."""
    path = session_env_path()
    try:
        _read(path)
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError(f"Smart Router session controls at {path} are invalid.") from exc
    atomic_write_json(path, _validate(dict(values)))
