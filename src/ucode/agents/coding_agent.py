"""Custom coding-agent attribution shared by agent adapters."""

from __future__ import annotations

import os
from collections.abc import Mapping

from ucode.constants import (
    CUSTOM_CODING_AGENT_HEADER,
    UNITY_GATEWAY_CUSTOM_CODING_AGENT_ENV_VAR,
)
from ucode.ui import print_warning_err


def custom_coding_agent_headers(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the explicitly configured identity override, or no additional headers."""
    source = os.environ if env is None else env
    value = source.get(UNITY_GATEWAY_CUSTOM_CODING_AGENT_ENV_VAR) or ""
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        print_warning_err(
            f"Ignoring {UNITY_GATEWAY_CUSTOM_CODING_AGENT_ENV_VAR}: "
            "value contains control characters. Provide a single-line agent name."
        )
        return {}
    value = value.strip()
    return {CUSTOM_CODING_AGENT_HEADER: value} if value else {}
