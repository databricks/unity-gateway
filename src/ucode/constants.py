"""Shared UCode constants."""

LOCALHOST = "localhost"
LOOPBACK_HOST = "127.0.0.1"

ENABLE_SMART_ROUTING_ENV_VAR = "ENABLE_SMART_ROUTING_V2"
ENABLE_SUBAGENT_ROUTING_ENV_VAR = "ENABLE_SMART_ROUTING_SUBAGENT_ONLY"
ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR = "ENABLE_SMART_ROUTER_ORCHESTRATOR"
SMART_ROUTING_ENV_KEYS = (
    ENABLE_SMART_ROUTING_ENV_VAR,
    ENABLE_SUBAGENT_ROUTING_ENV_VAR,
)
SMART_ROUTER_DISABLED = "DISABLED"
SMART_ROUTER_DEFAULT_NAME = "task_v3"
SMART_ROUTER_RECIPE_LOCAL = "SMART_ROUTER_RECIPE_LOCAL"
SMART_ROUTER_RECIPE_FIELD = "smart_router_recipe_name"

MODEL_PROVIDER_SERVICE_HEADER = "Databricks-Model-Provider-Service"
MODEL_SERVICE_PARENT_SCHEMA_HEADER = "Databricks-Model-Service-Parent-Schema"
# Names the smart-router recipe (e.g. `task_v3`) in use; sent only when smart routing is enabled.
SMART_ROUTER_RECIPE_HEADER = "Databricks-Smart-Router-Recipe"

# MCP server registration scopes. Claude Code supports local/project/user; the
# other CLIs only take the user-scope name. Kept here (a leaf module) so both
# `ucode.mcp` and `ucode.agents.claude` can import them without an import cycle.
MCP_USER_SCOPE = "user"
MCP_CLEANUP_SCOPES = ("local", "project", MCP_USER_SCOPE)
