"""Validation helpers for model-discovery evidence."""

import re

_CLAUDE_GATEWAY_ALIAS = re.compile(r"^anthropic-aigw-[0-9a-fA-F]{8}-(?P<model>.*)$")
_CLAUDE_PICKER_ROW = re.compile(r"(?m)^[ \t]*(?:[❯›>][ \t]*)?\d+\.[ \t]+(?P<label>[^\n]*)$")
_CLAUDE_NATIVE_PICKER_LABELS = {
    "claude-haiku-4-5": ("Haiku", "Haiku 4.5"),
    "claude-opus-5": ("Opus", "Opus 5"),
    "claude-sonnet-5": ("Sonnet", "Sonnet 5"),
}
_SYSTEM_AI_PREFIX = "system.ai."


def claude_model_in_picker(screen: str, model_id: str, display_name: str | None) -> bool:
    """Return whether a Claude model appears in a numbered picker row.

    The captured terminal screen also contains startup banners and picker
    footer text.  Restrict matching to rows with a numeric picker position so
    those other screen regions cannot satisfy the assertion accidentally.
    """
    rows = [row.group("label") for row in _CLAUDE_PICKER_ROW.finditer(screen)]
    if model_id == "claude-haiku-4-5-20251001":
        # Claude can deduplicate this gateway model into its built-in Haiku 4.5 row. An explicit
        # provider or parent catalog can instead render the raw model id, so fall through to the
        # generic ID/display-name check when the native row is absent.
        if any(re.search(r"^Haiku\b[^\n]*\bHaiku 4\.5\b", row) for row in rows):
            return True
    if model_id in _CLAUDE_NATIVE_PICKER_LABELS:
        # Bare native IDs can also be deduplicated into built-in family rows.
        # Match the exact family/version, never a banner or another native version.
        family, label = _CLAUDE_NATIVE_PICKER_LABELS[model_id]
        pattern = rf"^{re.escape(family)}\b[^\n]*\b{re.escape(label)}\b(?!\.\d)"
        if any(re.search(pattern, row) for row in rows):
            return True
    candidates = [
        value for value in (model_id, display_name) if isinstance(value, str) and value.strip()
    ]
    if not candidates:
        return False
    return any(any(candidate in row for candidate in candidates) for row in rows)


def claude_system_model_ids(models: list[dict]) -> list[str]:
    """Return canonical ``system.ai`` ids from Claude's gateway model entries.

    Claude Code may expose a gateway alias for a Databricks-hosted model.  The
    alias is only valid when its provider prefix has exactly eight hexadecimal
    characters; all other entries must already be canonical ``system.ai`` ids.
    Raw ids are checked for duplicates before aliases are normalized so that
    malformed or lossy evidence cannot be silently hidden by normalization.
    """
    assert isinstance(models, list) and models, f"Expected a non-empty model list: {models!r}"

    raw_ids: list[str] = []
    for model in models:
        assert isinstance(model, dict), f"Invalid model entry: {model!r}"
        model_id = model.get("id")
        assert isinstance(model_id, str) and model_id, f"Invalid model id: {model!r}"
        raw_ids.append(model_id)

    assert len(raw_ids) == len(set(raw_ids)), f"Duplicate raw model ids: {models!r}"

    canonical_ids: list[str] = []
    for model_id in raw_ids:
        alias = _CLAUDE_GATEWAY_ALIAS.fullmatch(model_id)
        canonical_id = alias.group("model") if alias else model_id
        assert canonical_id.startswith(_SYSTEM_AI_PREFIX) and len(canonical_id) > len(
            _SYSTEM_AI_PREFIX
        ), f"Expected a system.ai model id: {model_id!r}"
        canonical_ids.append(canonical_id)
    return canonical_ids
