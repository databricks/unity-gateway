"""Claude-specific helpers for native settings values."""

from __future__ import annotations

import json


def merge_extra_body(raw: str | None, updates: dict) -> str:
    """Merge owned top-level request fields while retaining unrelated caller fields."""
    try:
        payload = json.loads(raw) if raw is not None else {}
    except (ValueError, TypeError) as exc:
        raise RuntimeError("CLAUDE_CODE_EXTRA_BODY must contain a valid JSON object.") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("CLAUDE_CODE_EXTRA_BODY must contain a valid JSON object.")
    payload.update(updates)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
