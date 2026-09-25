"""Shared string helpers."""

from __future__ import annotations

from datetime import UTC, datetime


def parse_update_time(value: str | None) -> datetime | None:
    """Parse an RFC-3339 ``update_time`` into an aware UTC datetime, or None if absent/unparseable.

    UC serializes fractional seconds only when non-zero (protobuf JSON), so ``...:25Z`` and
    ``...:25.400Z`` both occur; parsing before comparing avoids the wrong lexicographic ordering of
    those two forms. An offset-less value is pinned to UTC.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def is_valid_catalog_schema(value: str) -> bool:
    """Return whether value is a safe ``<catalog>.<schema>`` reference."""
    parts = value.split(".")
    return len(parts) == 2 and all(
        part
        and part.isprintable()
        and not any(character.isspace() or character == "/" for character in part)
        for part in parts
    )
