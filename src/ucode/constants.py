"""Shared UCode constants."""

import os

LOCALHOST = "localhost"
LOOPBACK_HOST = "127.0.0.1"

MODEL_PROVIDER_SERVICE_HEADER = "Databricks-Model-Provider-Service"
MODEL_SERVICE_PARENT_SCHEMA_HEADER = "Databricks-Model-Service-Parent-Schema"

# Public policy switch for agent-owned catalogs scoped by a Model Provider
# Service or Unity Catalog location. Ordinary ``system.ai`` discovery is
# intentionally independent of this flag.
MODEL_DISCOVERY_ENV_VAR = "UG_ENABLE_MODEL_DISCOVERY"


def scoped_model_discovery_enabled(
    *,
    override: bool | None = None,
    force: bool = False,
) -> bool:
    """Whether provider/location-scoped agent discovery should be enabled.

    ``override`` is transient launch state, not persisted configuration. Managed
    workspace policy uses ``force`` because it outranks a developer's environment.
    """
    if force:
        return True
    if override is not None:
        return override
    return os.environ.get(MODEL_DISCOVERY_ENV_VAR) != "0"


# MCP server registration scopes. Claude Code supports local/project/user; the
# other CLIs only take the user-scope name. Kept here (a leaf module) so both
# `ucode.mcp` and `ucode.agents.claude` can import them without an import cycle.
MCP_USER_SCOPE = "user"
MCP_CLEANUP_SCOPES = ("local", "project", MCP_USER_SCOPE)
