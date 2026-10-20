"""Extra request headers from ``ug --header`` / ``UG_HEADERS``.

Lives outside ``ucode.agents`` so ``ucode.databricks``, which that package imports, can import it.
"""

from __future__ import annotations

import os
import re

from ucode.constants import UG_HEADERS_ENV_VAR

_HEADER_NAME_RE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


def ug_headers() -> dict[str, str]:
    """``UG_HEADERS`` as ``{name: value}``, one ``Name: value`` per line."""
    headers: dict[str, str] = {}
    for line in os.environ.get(UG_HEADERS_ENV_VAR, "").splitlines():
        if not line.strip():
            continue
        name, separator, value = line.partition(":")
        if not separator or not _HEADER_NAME_RE.fullmatch(name.strip()) or not value.strip():
            raise RuntimeError(f"{UG_HEADERS_ENV_VAR}: expected `Name: value`, got {line!r}.")
        headers[name.strip()] = value.strip()
    return headers
