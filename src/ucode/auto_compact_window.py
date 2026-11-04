"""Claude Code ``autoCompactWindow`` bounds shared by ug and its dependency-light hook."""

from __future__ import annotations

# The `[1m]` hint makes Claude Code size GPT sessions at 1M, but the gateway rejects them near 920K
# input tokens with an error Claude Code's reactive compaction doesn't recognize. Claude Code
# compacts ~33K below this window and only between turns, so 800K leaves room for a large turn.
GPT_AUTO_COMPACT_WINDOW = 800_000

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
