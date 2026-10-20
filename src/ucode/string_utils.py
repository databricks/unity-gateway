"""Shared string validation helpers."""

from __future__ import annotations


def _is_valid_dotted_name(value: str, levels: int) -> bool:
    parts = value.split(".")
    return len(parts) == levels and all(
        part
        and part.isprintable()
        and not any(character.isspace() or character == "/" for character in part)
        for part in parts
    )


def is_valid_catalog_schema(value: str) -> bool:
    """Return whether value is a safe ``<catalog>.<schema>`` reference."""
    return _is_valid_dotted_name(value, 2)


def is_valid_catalog_schema_name(value: str) -> bool:
    """Return whether value is a safe ``<catalog>.<schema>.<name>`` reference."""
    return _is_valid_dotted_name(value, 3)
