"""Validated local-file environment values, applied only to agent children."""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Mapping

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
ORIGINAL_PATH_ENV = "UCODE_ORIGINAL_PATH"
# Helpers inherit the agent environment, so their auth and configuration selectors
# must remain consistent with UG's own authentication and destination planning.
_RESERVED = frozenset(
    {
        ORIGINAL_PATH_ENV,
        "PATHEXT",
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
        "OAUTH_TOKEN",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_CUSTOM_HEADERS",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_MODEL",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_API_KEY_HELPER_TTL_MS",
        "CLAUDE_CODE_USE_GATEWAY",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY",
        "ENABLE_CLAUDE_CODE_GATEWAY_MODEL_DISCOVERY",
        "ENABLE_SMART_ROUTING_V2",
        "ENABLE_SMART_ROUTING_SUBAGENT_ONLY",
        "UCODE_CLAUDE_V2_SOCKET",
        "SMART_ROUTER_NAME",
        "ENABLE_CUSTOM_OAUTH_FROM_CLI",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "DATABRICKS_BEARER",
        "DATABRICKS_BEARER_COMMAND",
        "DATABRICKS_HOST",
        "DATABRICKS_TOKEN",
        "DATABRICKS_CONFIG_PROFILE",
        "DATABRICKS_CONFIG_FILE",
        "DATABRICKS_AUTH_TYPE",
        "DATABRICKS_AUTH_STORAGE",
        "DATABRICKS_CLIENT_ID",
        "DATABRICKS_CLIENT_SECRET",
        "DATABRICKS_ACCOUNT_ID",
        "UCODE_MANAGED_CONFIG_STUB",
        "UG_WORKSPACE",
        "UCODE_WEB_SEARCH_MODEL",
    }
    | {
        f"ANTHROPIC_DEFAULT_{family}_MODEL{suffix}"
        for family in ("FABLE", "OPUS", "SONNET", "HAIKU")
        for suffix in ("", "_NAME")
    }
)
_TRACE_CONTROLS = frozenset(
    {
        "OTEL_TRACES_EXPORTER",
        "OTEL_SDK_DISABLED",
        "CLAUDE_CODE_ENABLE_TELEMETRY",
        "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA",
        "CLAUDE_CODE_OTEL_HEADERS_HELPER_DEBOUNCE_MS",
        "CLAUDE_CODE_PROPAGATE_TRACEPARENT",
    }
    | {
        f"OTEL_EXPORTER_OTLP_{signal}{field}"
        for signal in ("", "TRACES_")
        for field in (
            "ENDPOINT",
            "PROTOCOL",
            "HEADERS",
            "CERTIFICATE",
            "CLIENT_CERTIFICATE",
            "CLIENT_KEY",
        )
    }
)


def validate_env_name(name: object, path: str) -> str:
    """Validate a custom declaration or virtual handoff path without echoing values."""
    if not isinstance(name, str) or not _ENV_NAME.fullmatch(name):
        raise RuntimeError(
            f"Invalid --config-file at {path}: expected an environment variable name."
        )
    if name.upper() in _RESERVED:
        raise RuntimeError(
            f"Invalid --config-file at {path}.{name}: reserved environment variable."
        )
    return name


def env_name_identity(name: str) -> str:
    return name.upper() if os.name == "nt" else name


def _environment_value(environment: Mapping[str, str], name: str) -> str | None:
    return next(
        (value for key, value in environment.items() if env_name_identity(key) == name), None
    )


def auth_dependency_env(inherited: Mapping[str, str] | None = None) -> dict[str, str] | None:
    """Restore UG's executable search path only for its authentication dependencies."""
    environment = os.environ if inherited is None else inherited
    original_path = _environment_value(environment, ORIGINAL_PATH_ENV)
    if original_path is None:
        return None
    restored = {
        name: value for name, value in environment.items() if env_name_identity(name) != "PATH"
    }
    restored["PATH"] = original_path
    return restored


def _validate_trace_conflicts(values: Mapping[str, str], path: str, enabled: bool) -> None:
    if enabled:
        conflicts = sorted(name for name in values if name.upper() in _TRACE_CONTROLS)
        if conflicts:
            raise RuntimeError(
                f"Invalid --config-file at {path}: environment variables conflict with enabled UG "
                f"tracing: {', '.join(conflicts)}. Disable UG tracing or remove those declarations."
            )


def validate_custom_env(value: object, path: str, tool: str, raw_config: dict) -> dict[str, str]:
    """Agent-extension parser for exact, file-only custom environment values."""
    if tool not in {"claude", "codex"}:
        raise RuntimeError(
            f"Invalid --config-file at {path}: custom_env supports claude and codex."
        )
    if not isinstance(value, dict):
        raise RuntimeError(f"Invalid --config-file at {path}: expected an object.")
    result = {}
    identities = set()
    for name, item in value.items():
        name = validate_env_name(name, path)
        identity = env_name_identity(name)
        if identity in identities:
            raise RuntimeError(
                f"Invalid --config-file at {path}.{name}: duplicate environment name."
            )
        identities.add(identity)
        if not isinstance(item, str) or "\x00" in item:
            raise RuntimeError(
                f"Invalid --config-file at {path}.{name}: expected a string without NUL."
            )
        try:
            item.encode("utf-8")
        except UnicodeEncodeError:
            raise RuntimeError(
                f"Invalid --config-file at {path}.{name}: expected valid Unicode."
            ) from None
        result[name] = item
    tracing = raw_config.get("tracing", {})
    if not isinstance(tracing, dict) or (
        "enabled" in tracing and type(tracing["enabled"]) is not bool
    ):
        raise RuntimeError(f"Invalid --config-file at {path}: expected a boolean tracing.enabled.")
    _validate_trace_conflicts(result, path, tracing.get("enabled") is True)
    return result


def agent_custom_env(state: dict, agent: str) -> dict[str, str]:
    return validate_custom_env(
        state.get(f"{agent}_custom_env", {}), f"{agent}.custom_env", agent, {}
    )


def validate_agent_env(state: dict, agent: str) -> None:
    _validate_trace_conflicts(
        agent_custom_env(state, agent),
        f"{agent}.custom_env",
        bool(state.get(f"{agent}_otel_tracing")),
    )


def build_child_env(
    state: dict,
    agent: str,
    *,
    generated: Mapping[str, str | None] | None = None,
    inherited: Mapping[str, str] | None = None,
) -> dict[str, str]:
    from ucode.managed_ownership import retired_environment

    validate_agent_env(state, agent)
    child = dict(os.environ if inherited is None else inherited)
    retired = {env_name_identity(name) for name in retired_environment(agent)}
    custom = agent_custom_env(state, agent)
    original_path = _environment_value(child, ORIGINAL_PATH_ENV)
    if original_path is None and "PATH" in retired | {env_name_identity(name) for name in custom}:
        original_path = _environment_value(child, "PATH")
        if original_path is None:
            original_path = os.defpath
    child = {name: value for name, value in child.items() if env_name_identity(name) not in retired}
    supplied = {**custom, **(generated or {})}
    if original_path is not None:
        supplied[ORIGINAL_PATH_ENV] = original_path
    for name, value in supplied.items():
        if os.name == "nt":
            for existing in list(child):
                if env_name_identity(existing) == env_name_identity(name):
                    child.pop(existing)
        if value is None:
            child.pop(name, None)
        else:
            child[name] = value
    return child


def resolve_agent_argv(argv: list[str], *, inherited: Mapping[str, str] | None = None) -> list[str]:
    """Resolve the initial executable before the child receives its custom PATH."""
    if not argv:
        raise RuntimeError("Cannot launch an agent without an executable.")
    environment = os.environ if inherited is None else inherited
    search_path = _environment_value(environment, "PATH")
    if search_path is None:
        search_path = os.defpath
    executable = shutil.which(argv[0], path=search_path)
    if executable is None:
        raise RuntimeError(f"Cannot find the agent executable {argv[0]}; install it and retry.")
    return [os.path.abspath(executable), *argv[1:]]
