"""Shared UCode constants."""

TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})

LOCALHOST = "localhost"
LOOPBACK_HOST = "127.0.0.1"

AGENT_CLAUDE = "claude"
AGENT_CODEX = "codex"

ENABLE_SMART_ROUTING_ENV_VAR = "ENABLE_SMART_ROUTING_V2"
ENABLE_SUBAGENT_ROUTING_ENV_VAR = "ENABLE_SMART_ROUTING_SUBAGENT_ONLY"
ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR = "ENABLE_SMART_ROUTER_ORCHESTRATOR"
SMART_ROUTER_CONFIG_VERSION_ENV_VAR = "SMART_ROUTER_CONFIG_VERSION"
ISAAC_SMART_ROUTER_FEEDBACK_ENV_VAR = "ISAAC_SMART_ROUTER_FEEDBACK"
SMART_ROUTING_ENV_KEYS = (
    ENABLE_SMART_ROUTING_ENV_VAR,
    ENABLE_SUBAGENT_ROUTING_ENV_VAR,
    ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR,
)
# Temporary test gate: eligible launches that start without routing still get the setup that
# `/smart-router on` needs. It isn't a routing flag, so it stays out of SMART_ROUTING_ENV_KEYS.
ENABLE_SMART_ROUTER_ON_DEMAND_ENV_VAR = "ENABLE_SMART_ROUTER_ON_DEMAND"

MODEL_PROVIDER_SERVICE_HEADER = "Databricks-Model-Provider-Service"
MODEL_SERVICE_PARENT_SCHEMA_HEADER = "Databricks-Model-Service-Parent-Schema"
# Names the smart-router recipe (e.g. `task_v3`) in use; sent only when smart routing is enabled.
SMART_ROUTER_RECIPE_HEADER = "Databricks-Smart-Router-Recipe"

# Attribution tags the gateway records against every inference call. The value is
# opaque to ug and set by the user via ``AI_GATEWAY_REQUEST_TAGS``; it is passed
# through verbatim so the gateway (not ug) validates the tag syntax. The helper that
# reads the env var lives in ``ucode.agents.request_tags`` (it needs ``ui``).
AI_GATEWAY_REQUEST_TAGS_HEADER = "Databricks-Ai-Gateway-Request-Tags"
AI_GATEWAY_REQUEST_TAGS_ENV_VAR = "AI_GATEWAY_REQUEST_TAGS"


# MCP server registration scopes. Claude Code supports local/project/user; the
# other CLIs only take the user-scope name. Kept here (a leaf module) so both
# `ucode.mcp` and `ucode.agents.claude` can import them without an import cycle.
MCP_USER_SCOPE = "user"
MCP_CLEANUP_SCOPES = ("local", "project", MCP_USER_SCOPE)
