"""Shared UCode constants."""

LOCALHOST = "localhost"
LOOPBACK_HOST = "127.0.0.1"

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
