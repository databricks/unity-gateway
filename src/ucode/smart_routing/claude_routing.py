"""Claude Code smart-routing hook helpers.

Claude-specific configuration on top of the shared
:mod:`ucode.smart_routing.routing` core: the spawn-tool detector, the subagent
notice, and the artifact paths. :mod:`ucode.smart_routing.v2` builds the requests.
"""

from __future__ import annotations

from typing import Any

from ucode.config_io import APP_DIR
from ucode.smart_routing import routing

# Claude Code's subagent-spawn tool (renamed Task -> Agent); match both.
SPAWN_AGENT_TOOL_NAMES = ("agent", "task")
CANARY_PATH = APP_DIR / "claude-smart-routing-canary.json"
AUDIT_PATH = APP_DIR / "claude-smart-routing-audit.jsonl"
DECISIONS_PATH = APP_DIR / "claude-smart-routing-decisions.jsonl"
SUBAGENT_NOTICE_CONFIG = routing.SubagentNoticeConfig(
    name_field="subagent_type",
    prompt_field="prompt",
    display_model_mapper=routing.unwrap_anthropic_gateway_model,
    leading_newline=True,
)


# Claude Code CLI options that consume a following value (from `claude --help`);
# their values must not be mistaken for the seed prompt. Options whose value is
# optional (`-c`/`-d`/`-r`/`-w`) are intentionally omitted — treating them as
# booleans is safe here (we only skip the flag, never a following positional).
CLAUDE_VALUE_OPTIONS = frozenset(
    {
        "-n",
        "--name",
        "-m",
        "--model",
        "--add-dir",
        "--agent",
        "--agents",
        "--allowed-tools",
        "--disallowed-tools",
        "--tools",
        "--append-system-prompt",
        "--system-prompt",
        "--betas",
        "--debug-file",
        "--effort",
        "--fallback-model",
        "--file",
        "--input-format",
        "--output-format",
        "--json-schema",
        "--max-budget-usd",
        "--mcp-config",
        "--permission-mode",
        "--plugin-dir",
        "--plugin-url",
        "--remote-control-session-name-prefix",
        "--session-id",
        "--setting-sources",
        "--settings",
    }
)


def is_spawn_agent_tool(tool_name: Any) -> bool:
    """Return whether a hook payload names Claude Code's subagent spawn tool."""
    if not isinstance(tool_name, str):
        return False
    return tool_name.strip().lower() in SPAWN_AGENT_TOOL_NAMES


def record_session_start(payload: dict[str, Any]) -> None:
    """Write a canary proving Claude Code trusted and ran the routing hooks."""
    routing.record_session_start(CANARY_PATH, payload)


def record_subagent_start(payload: dict[str, Any]) -> dict[str, Any]:
    """Append the model Claude Code actually selected for a routed subagent."""
    return routing.record_subagent_start(DECISIONS_PATH, AUDIT_PATH, payload)


def clear_routing_artifacts() -> None:
    """Remove ucode-owned routing canary and audit files."""
    routing.clear_artifacts((CANARY_PATH, AUDIT_PATH, DECISIONS_PATH))
