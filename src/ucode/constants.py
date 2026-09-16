"""Shared UCode constants."""

import os

LOCALHOST = "localhost"
LOOPBACK_HOST = "127.0.0.1"

MODEL_PROVIDER_SERVICE_HEADER = "Databricks-Model-Provider-Service"
MODEL_SERVICE_PARENT_SCHEMA_HEADER = "Databricks-Model-Service-Parent-Schema"

# Controls provider- and location-scoped agent catalogs.
MODEL_DISCOVERY_ENV_VAR = "UG_ENABLE_MODEL_DISCOVERY"


def scoped_model_discovery_enabled(
    *,
    managed_config_exists: bool = False,
) -> bool:
    """Return whether scoped model discovery is enabled."""
    return managed_config_exists or os.environ.get(MODEL_DISCOVERY_ENV_VAR) != "0"


# MCP server registration scopes. Claude Code supports local/project/user; the
# other CLIs only take the user-scope name. Kept here (a leaf module) so both
# `ucode.mcp` and `ucode.agents.claude` can import them without an import cycle.
MCP_USER_SCOPE = "user"
MCP_CLEANUP_SCOPES = ("local", "project", MCP_USER_SCOPE)
