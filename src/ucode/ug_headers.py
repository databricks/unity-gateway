"""Extra request headers from ``ug --header`` / ``UG_HEADERS``.

Lives outside ``ucode.agents`` so ``ucode.databricks``, which that package imports, can import it.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

from ucode.constants import CLIENT_SURFACES_HEADER, UG_HEADERS_ENV_VAR

_HEADER_NAME_RE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_CLIENT_SURFACES_KEY = CLIENT_SURFACES_HEADER.casefold()


def _is_sendable_value(value: str) -> bool:
    """No control characters, and only Latin-1, which urllib and httpx encode header values as."""
    return all(char == "\t" or " " <= char <= "~" or "\x80" <= char <= "\xff" for char in value)


def parse_header_line(line: str) -> tuple[str, str] | None:
    """``(name, value)`` for one ``Name: value`` line, or ``None`` when it isn't a valid header."""
    name, separator, value = line.partition(":")
    name, value = name.strip(), value.strip()
    if not separator or not _HEADER_NAME_RE.fullmatch(name) or not value:
        return None
    return (name, value) if _is_sendable_value(value) else None


def merge_client_surfaces(*values: str) -> str:
    """Combine client surfaces in order, ignoring repeated names regardless of casing."""
    surfaces: dict[str, str] = {}
    for value in values:
        for item in value.split(","):
            if surface := item.strip():
                surfaces.setdefault(surface.casefold(), surface)
    return ", ".join(surfaces.values())


def _merge_header(headers: dict[str, tuple[str, str]], name: str, value: str) -> None:
    key = name.casefold()
    if key == _CLIENT_SURFACES_KEY and key in headers:
        name, previous = headers[key]
        value = merge_client_surfaces(previous, value)
    headers[key] = (name, value)


def ug_headers(existing: Mapping[str, str] | None = None) -> dict[str, str]:
    """Merge ``UG_HEADERS`` and launch attribution with existing request headers.

    Invalid lines are skipped, so a bad inherited value never blocks a command. A later line
    replaces an earlier one with the same name in any case, so ``--header`` overrides an inherited
    value. Existing headers win, except client surfaces are additive. An OpenUI session contributes
    its surface without persisting the marker or copying its session id into a request.
    Spread it first into a urllib header dict so ug's own headers listed after it win."""
    headers: dict[str, tuple[str, str]] = {}
    for name, value in (existing or {}).items():
        _merge_header(headers, name, value)
    protected = set(headers)
    for line in os.environ.get(UG_HEADERS_ENV_VAR, "").splitlines():
        parsed = parse_header_line(line)
        if parsed:
            name, value = parsed
            if name.casefold() not in protected or name.casefold() == _CLIENT_SURFACES_KEY:
                _merge_header(headers, name, value)
    if os.environ.get("OPENUI_SESSION_ID", "").strip():
        _merge_header(headers, CLIENT_SURFACES_HEADER, "openui")
    return dict(headers.values())
