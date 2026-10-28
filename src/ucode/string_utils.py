"""Shared string helpers."""

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


_MODEL_LABEL_ACRONYMS = frozenset({"glm", "gpt"})


def model_picker_label(model: str) -> str:
    """A human-friendly picker label for a model id (e.g. ``system.ai.claude-haiku-4-5`` ->
    ``Claude Haiku 4.5``): keep the vendor and name words title-cased, uppercase known acronyms,
    and join a run of numeric segments into a dotted version."""
    stem = model.removeprefix("system.ai.")
    parts: list[str] = []
    version: list[str] = []
    for token in stem.split("-"):
        if token.isdigit():
            version.append(token)
            continue
        if version:
            parts.append(".".join(version))
            version = []
        parts.append(token.upper() if token in _MODEL_LABEL_ACRONYMS else token.title())
    if version:
        parts.append(".".join(version))
    return " ".join(parts) if parts else stem
