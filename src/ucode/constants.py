"""Shared UCode constants."""

LOCALHOST = "localhost"
LOOPBACK_HOST = "127.0.0.1"

MODEL_PROVIDER_SERVICE_HEADER = "Databricks-Model-Provider-Service"
MODEL_SERVICE_PARENT_SCHEMA_HEADER = "Databricks-Model-Service-Parent-Schema"
# Names the smart-router recipe (e.g. `task_v3`) in use; sent only when smart routing is enabled.
SMART_ROUTER_RECIPE_HEADER = "Databricks-Smart-Router-Recipe"

# Public policy switch for agent-native catalogs scoped by a Model Provider
# Service or Unity Catalog location.  Ordinary ``system.ai`` discovery is
# intentionally independent of this flag.
MODEL_DISCOVERY_ENV_VAR = "UG_ENABLE_MODEL_DISCOVERY"

# Launch-only state handed from the CLI to Codex. Routing headers remain active
# when this is false; only the scoped ``model_catalog_json`` refresh is skipped.
CODEX_SCOPED_MODEL_DISCOVERY_STATE_KEY = "_codex_scoped_model_discovery"

# MCP server registration scopes. Claude Code supports local/project/user; the
# other CLIs only take the user-scope name. Kept here (a leaf module) so both
# `ucode.mcp` and `ucode.agents.claude` can import them without an import cycle.
MCP_USER_SCOPE = "user"
MCP_CLEANUP_SCOPES = ("local", "project", MCP_USER_SCOPE)
