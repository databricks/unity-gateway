"""Helpers for reading the model inventory ug keeps in state.

Deliberately stdlib-only, so every agent module can import it without pulling in the rest of ug.
"""

from __future__ import annotations


def model_values(value: object) -> list[str]:
    """Flatten a state model inventory (str, list, or provider-keyed dict) into model ids."""
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item]
    if isinstance(value, dict):
        return [model for models in value.values() for model in model_values(models)]
    return []
