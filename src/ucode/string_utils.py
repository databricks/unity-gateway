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
    and join a run of numeric segments into a dotted version.

    A ``system.ai`` namespace is dropped. Another dotted namespace (e.g. an MPS source such as
    ``main.anthropic.byok.claude-opus-5-5``) is kept as a suffix so two sources offering the same
    model stay distinct: ``Claude Opus 5.5 (main.anthropic.byok)``. A dot that is part of the model
    name itself is not treated as a namespace separator: the split happens only when the last
    dot-separated segment looks like a dash-separated model name rather than a version tail, so
    ``gpt-5.5`` and ``system.ai.llama-3.3-70b-instruct`` keep their current labels."""
    namespace = None
    stem = model
    if "." in model:
        head, _, last = model.rpartition(".")
        if "-" in last and not last[0].isdigit():
            # Everything before the final dot is a catalog/schema-style namespace.
            stem = last
            if head != "system.ai":
                namespace = head
    if namespace is None:
        stem = stem.removeprefix("system.ai.")
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
    label = " ".join(parts) if parts else stem
    return f"{label} ({namespace})" if namespace else label
