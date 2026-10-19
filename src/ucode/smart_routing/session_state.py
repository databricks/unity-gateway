"""Read a session's Smart Router switch without importing the rest of ug.

Hooks run on every prompt and subagent start, so a session that is off must cost no more
than a file read. Keep this module limited to the standard library.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping

SESSION_ENV_VAR = "UCODE_SESSION_ENV_FILE"
SWITCH_KEYS = frozenset({"enabled", "when_on", "ever_enabled"})


def is_switch(controls: object) -> bool:
    """Whether session controls use the switch format rather than earlier flat overrides."""
    return isinstance(controls, dict) and controls.keys() == SWITCH_KEYS


def read_switch(env: Mapping[str, str] | None = None) -> dict | None:
    """Return the session's switch, or None when there is no readable switch to trust."""
    source = os.environ if env is None else env
    path = source.get(SESSION_ENV_VAR, "").strip()
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as file:
            controls = json.load(file)
    except (OSError, ValueError):
        return None
    if (
        is_switch(controls)
        and isinstance(controls["enabled"], bool)
        and isinstance(controls["ever_enabled"], bool)
        and isinstance(controls["when_on"], dict)
    ):
        return controls
    return None


def is_off(env: Mapping[str, str] | None = None) -> bool:
    switch = read_switch(env)
    return switch is not None and not switch["enabled"]
