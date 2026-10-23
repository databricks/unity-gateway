"""Extra request headers from ``ug --header`` / ``UG_HEADERS``.

Lives outside ``ucode.agents`` so ``ucode.databricks``, which that package imports, can import it.
"""

from __future__ import annotations

import os
import re

from ucode.constants import UG_HEADERS_ENV_VAR

_HEADER_NAME_RE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


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


def invalid_ug_header_lines() -> list[int]:
    """The 1-based numbers of the non-blank ``UG_HEADERS`` lines that aren't valid headers."""
    lines = os.environ.get(UG_HEADERS_ENV_VAR, "").splitlines()
    return [n for n, line in enumerate(lines, 1) if line.strip() and not parse_header_line(line)]


def ug_headers() -> dict[str, str]:
    """``UG_HEADERS`` as ``{name: value}``, one ``Name: value`` per line.

    Invalid lines are skipped (the CLI warns about them once). A later line replaces an earlier
    one with the same name in any case, so ``--header`` overrides an inherited value."""
    headers: dict[str, tuple[str, str]] = {}
    for line in os.environ.get(UG_HEADERS_ENV_VAR, "").splitlines():
        parsed = parse_header_line(line)
        if parsed:
            headers[parsed[0].casefold()] = parsed
    return dict(headers.values())


def with_ug_headers(headers: dict[str, str]) -> dict[str, str]:
    """``headers`` plus each ``UG_HEADERS`` header it doesn't already set, in any case.

    ug's own headers (``Authorization``, ``Content-Type``, a caller's) always keep their value."""
    present = {name.casefold() for name in headers}
    extra = {name: value for name, value in ug_headers().items() if name.casefold() not in present}
    return {**headers, **extra}
