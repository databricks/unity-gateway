"""Claude Code ``autoCompactWindow`` bounds used by ug."""

from __future__ import annotations

import re

# The `[1m]` hint makes Claude Code size a non-Claude session at 1M, but the gateway rejects them
# earlier (GPT near 920K; GLM and Kimi accept ~850K), with an error Claude Code's reactive
# compaction doesn't recognize. Claude Code compacts ~33K below the window and only between turns,
# so it can overshoot by one large turn; hence 800K.
AUTO_COMPACT_CAP = 800_000

_MODEL_ID_PREFIX_RE = re.compile(
    r"^(?:anthropic-aigw-[0-9a-fA-F]{8}-)?(?:system\.ai\.)?(?:databricks-)?"
)


def strip_model_id_prefixes(model: str) -> str:
    """``model`` without its `[1m]` hint and the gateway wrapper, `system.ai.`, and `databricks-`."""
    return _MODEL_ID_PREFIX_RE.sub("", model.removesuffix("[1m]"), count=1)


# Claude Code's accepted `autoCompactWindow` range; values outside it are not a usable choice.
CLAUDE_AUTO_COMPACT_WINDOW_MIN = 100_000
CLAUDE_AUTO_COMPACT_WINDOW_MAX = 1_000_000


def valid_auto_compact_window(value: object) -> int | None:
    """``value`` as an ``autoCompactWindow`` Claude Code accepts, or None."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return (
        value if CLAUDE_AUTO_COMPACT_WINDOW_MIN <= value <= CLAUDE_AUTO_COMPACT_WINDOW_MAX else None
    )


def user_auto_compact_windows(settings: object) -> dict[str, int]:
    """Valid ``autoCompactWindow`` values per id without `[1m]`; the lower spelling wins."""
    model_settings = settings.get("modelSettings") if isinstance(settings, dict) else None
    if not isinstance(model_settings, dict):
        return {}
    windows: dict[str, int] = {}
    for model, entry in model_settings.items():
        if not isinstance(model, str) or not isinstance(entry, dict):
            continue
        window = valid_auto_compact_window(entry.get("autoCompactWindow"))
        if window is not None:
            canonical = model.removesuffix("[1m]")
            windows[canonical] = min(windows.get(canonical, window), window)
    return windows
