"""Request-tag header helper shared by coding-agent launchers."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping

from ucode.constants import AI_GATEWAY_REQUEST_TAGS_ENV_VAR
from ucode.ui import print_warning_err


def request_tags_header_value(env: Mapping[str, str] | None = None) -> str | None:
    """The ``Databricks-Ai-Gateway-Request-Tags`` value for this session, or None.

    Reads ``AI_GATEWAY_REQUEST_TAGS`` (defaulting to the process environment) and
    returns its stripped value, or None when it is unset or blank so callers can
    simply skip the header.

    A value containing a line break cannot be sent as a single HTTP header: for
    Claude it would spill into extra header lines, and other agents' HTTP clients
    reject it before the request reaches the gateway. Such values are warned about
    and skipped (returns None) rather than silently corrupting the request."""
    source = os.environ if env is None else env
    value = (source.get(AI_GATEWAY_REQUEST_TAGS_ENV_VAR) or "").strip()
    if not value:
        return None
    try:
        # Make sure the value is a JSON object
        parsed = json.loads(value)
        if type(parsed) is not dict:
            print_warning_err(
                f"Ignoring {AI_GATEWAY_REQUEST_TAGS_ENV_VAR}: expected a JSON object, but got a {type(parsed)}"
            )
            return None
        return value
    except Exception as e:
        print_warning_err(
            f"Ignoring {AI_GATEWAY_REQUEST_TAGS_ENV_VAR}: value is not valid JSON. Provide the tags as JSON on a single line.\n{e}"
        )
        return None
