"""Shared UCode constants."""

import os
from collections.abc import Mapping

LOCALHOST = "localhost"
LOOPBACK_HOST = "127.0.0.1"

# Values an on/off env var may use to mean "on" (compare after .strip().lower()).
TRUTHY_ENV_VALUES = frozenset({"1", "true"})

ENABLE_SMART_ROUTING_ENV_VAR = "ENABLE_SMART_ROUTING_V2"
ENABLE_SUBAGENT_ROUTING_ENV_VAR = "ENABLE_SMART_ROUTING_SUBAGENT_ONLY"
SMART_ROUTING_ENV_KEYS = (
    ENABLE_SMART_ROUTING_ENV_VAR,
    ENABLE_SUBAGENT_ROUTING_ENV_VAR,
)

ENABLE_CLAUDE_CODE_MODS_ENV_VAR = "ENABLE_CLAUDE_CODE_MODS"


def claude_code_mods_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether the Claude Code mods experience is enabled."""
    source = os.environ if env is None else env
    return source.get(ENABLE_CLAUDE_CODE_MODS_ENV_VAR, "").strip().lower() in TRUTHY_ENV_VALUES


MODEL_PROVIDER_SERVICE_HEADER = "Databricks-Model-Provider-Service"
MODEL_SERVICE_PARENT_SCHEMA_HEADER = "Databricks-Model-Service-Parent-Schema"
# Names the smart-router recipe (e.g. `task_v3`) in use; sent only when smart routing is enabled.
SMART_ROUTER_RECIPE_HEADER = "Databricks-Smart-Router-Recipe"

# MCP server registration scopes. Claude Code supports local/project/user; the
# other CLIs only take the user-scope name. Kept here (a leaf module) so both
# `ucode.mcp` and `ucode.agents.claude` can import them without an import cycle.
MCP_USER_SCOPE = "user"
MCP_CLEANUP_SCOPES = ("local", "project", MCP_USER_SCOPE)
