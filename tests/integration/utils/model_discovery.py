"""Validation helpers for model-discovery evidence."""

import re

_CLAUDE_GATEWAY_ALIAS = re.compile(r"^anthropic-aigw-[0-9a-fA-F]{8}-(?P<model>.*)$")
_SYSTEM_AI_PREFIX = "system.ai."


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
