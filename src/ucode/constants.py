"""Shared UCode constants."""

import os
from collections.abc import Mapping

LOCALHOST = "localhost"
LOOPBACK_HOST = "127.0.0.1"

MODEL_PROVIDER_SERVICE_HEADER = "Databricks-Model-Provider-Service"
MODEL_SERVICE_PARENT_SCHEMA_HEADER = "Databricks-Model-Service-Parent-Schema"
# Names the smart-router recipe (e.g. `task_v3`) in use; sent only when smart routing is enabled.
SMART_ROUTER_RECIPE_HEADER = "Databricks-Smart-Router-Recipe"

# Attribution tags the gateway records against every inference call. The value is
# opaque to ug and set by the user via ``AI_GATEWAY_REQUEST_TAGS``; it is passed
# through verbatim so the gateway (not ug) validates the tag syntax.
AI_GATEWAY_REQUEST_TAGS_HEADER = "Databricks-Ai-Gateway-Request-Tags"
AI_GATEWAY_REQUEST_TAGS_ENV_VAR = "AI_GATEWAY_REQUEST_TAGS"


def request_tags_header_value(env: Mapping[str, str] | None = None) -> str | None:
    """The ``Databricks-Ai-Gateway-Request-Tags`` value for this session, or None.

    Reads ``AI_GATEWAY_REQUEST_TAGS`` (defaulting to the process environment) and
    returns its stripped value, or None when it is unset or blank so callers can
    simply skip the header."""
    source = os.environ if env is None else env
    value = (source.get(AI_GATEWAY_REQUEST_TAGS_ENV_VAR) or "").strip()
    return value or None


# MCP server registration scopes. Claude Code supports local/project/user; the
# other CLIs only take the user-scope name. Kept here (a leaf module) so both
# `ucode.mcp` and `ucode.agents.claude` can import them without an import cycle.
MCP_USER_SCOPE = "user"
MCP_CLEANUP_SCOPES = ("local", "project", MCP_USER_SCOPE)
