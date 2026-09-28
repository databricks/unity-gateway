"""Invocation-scoped local coding-agent configuration, separate from the API cache."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn
from urllib.parse import urlsplit

from ucode import config_io
from ucode.databricks import ANTHROPIC_FAMILIES
from ucode.managed_config import (
    AGENT_ENUM_TO_TOOL,
    AGENT_NAME_TO_TOOL,
    MAX_SPEC_VERSION,
    normalize_managed_config,
)

_METADATA = {
    "name",
    "workspace_id",
    "create_time",
    "created_user_id",
    "update_time",
    "updated_user_id",
    "retrieved_time",
}
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_RESERVED_ENV_NAMES = {
    "PATH",
    "PATHEXT",
    "HOME",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "SHELL",
    "COMSPEC",
    "ENV",
    "BASH_ENV",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "NODE_OPTIONS",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "OAUTH_TOKEN",
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
    "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
    "CLAUDE_CODE_GATEWAY_TOKEN_FILE_DESCRIPTOR",
    "CLAUDE_CODE_WEBSOCKET_AUTH_FILE_DESCRIPTOR",
    "CLAUDE_CODE_HOST_AUTH_ENV_VAR",
    "CLAUDE_CODE_HOST_CREDS_FILE",
    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
    "CLAUDE_CODE_OAUTH_SCOPES",
    "CLAUDE_CODE_OAUTH_CLIENT_ID",
    "CLAUDE_CODE_CUSTOM_OAUTH_URL",
    "CLAUDE_CODE_SESSION_ACCESS_TOKEN",
    "CLAUDE_SESSION_INGRESS_TOKEN_FILE",
    "CLAUDE_BG_AUTH_SNAPSHOT_PATH",
    "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
    "CLAUDE_CODE_API_KEY_HELPER_TTL_MS",
    "CLAUDE_CODE_USE_GATEWAY",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY",
    "ENABLE_CLAUDE_CODE_GATEWAY_MODEL_DISCOVERY",
    "ENABLE_CUSTOM_OAUTH_FROM_CLI",
    "ENABLE_SMART_ROUTING",
    "ENABLE_SMART_ROUTING_V2",
    "ENABLE_SMART_ROUTING_SUBAGENT_ONLY",
    "SMART_ROUTER_NAME",
}
_RESERVED_ENV_PREFIXES = (
    "DATABRICKS_",
    "ANTHROPIC_",
    "OPENAI_",
    "CODEX_",
    "UCODE_",
    "UG_",
    "XDG_",
    "BUNDLE_",
    "DYLD_",
)


@dataclass(frozen=True)
class SelectedManagedSource:
    """One validated file snapshot, reused throughout a launch without API fallback."""

    workspace: str
    agent: str
    _manifest_json: str = field(repr=False)

    @property
    def manifest(self) -> dict:
        return json.loads(self._manifest_json)

    @property
    def has_models(self) -> bool:
        return bool(self.manifest["enabled_agents"][self.agent].get("model_config"))

    @property
    def custom_env(self) -> dict[str, str]:
        return self.manifest["enabled_agents"][self.agent].get("custom_env", {})

    def check_target(self, workspace: str, agent: str | None = None) -> None:
        if self.workspace != workspace or (agent is not None and self.agent != agent):
            raise RuntimeError("Selected configuration does not match this launch target.")


def _invalid(path: str, message: str) -> NoReturn:
    raise RuntimeError(f"Invalid --config-file at {path}: {message}.")


def _object(value: object, path: str) -> dict:
    if not isinstance(value, dict):
        _invalid(path, "expected an object")
    return value


def _fields(value: dict, allowed: set[str], path: str) -> None:
    for key in value:
        if key not in allowed:
            _invalid(f"{path}.{key}", "unsupported field")


def _string(value: object, path: str, *, blank: bool = False) -> str:
    if not isinstance(value, str) or (not blank and not value.strip()):
        _invalid(path, "expected a nonempty string" if not blank else "expected a string")
    return value


def _strings(value: object, path: str, *, blank_values: bool = False) -> dict:
    mapping = _object(value, path)
    for key, item in mapping.items():
        _string(key, path)
        _string(item, f"{path}.{key}", blank=blank_values)
    return mapping


def _agent(value: object, path: str) -> str:
    name = _string(value, path)
    tool = AGENT_ENUM_TO_TOOL.get(name) or AGENT_NAME_TO_TOOL.get(name)
    if tool is None:
        _invalid(path, "unknown agent")
    return tool


def _fqn(value: object, path: str, parts: int) -> str:
    name = _string(value, path)
    if len(name.split(".")) != parts or not all(part.strip() for part in name.split(".")):
        _invalid(path, f"expected a {parts}-part Unity Catalog name")
    return name


def _validate_agent(value: object, path: str, tool: str) -> None:
    config = _object(value, path)
    _fields(
        config,
        {"models", "default_models", "http_headers", "smart_routing", "tracing", "custom_env"},
        path,
    )
    if "custom_env" in config:
        if tool not in {"claude", "codex"}:
            _invalid(
                f"{path}.custom_env", "only Claude and Codex support launch environment overrides"
            )
        custom_env = _object(config["custom_env"], f"{path}.custom_env")
        names: set[str] = set()
        for key, item in custom_env.items():
            if not isinstance(key, str) or not _ENV_NAME.fullmatch(key):
                _invalid(f"{path}.custom_env", "expected portable environment variable names")
            name = key.upper()
            if name in names:
                _invalid(f"{path}.custom_env.{key}", "duplicate environment name ignoring case")
            names.add(name)
            if name in _RESERVED_ENV_NAMES or name.startswith(_RESERVED_ENV_PREFIXES):
                _invalid(
                    f"{path}.custom_env.{key}",
                    "reserved for UG authentication or runtime configuration",
                )
            if not isinstance(item, str) or "\0" in item:
                _invalid(f"{path}.custom_env.{key}", "expected a string without NUL characters")
    defaults = _strings(config.get("default_models", {}), f"{path}.default_models")
    for key in defaults:
        if key != "default_model" and not (
            tool == "claude" and key in {f"default_{family}_model" for family in ANTHROPIC_FAMILIES}
        ):
            _invalid(f"{path}.default_models.{key}", "unsupported default model key for this agent")
    if "http_headers" in config:
        _strings(config["http_headers"], f"{path}.http_headers", blank_values=True)
    for flag in ("smart_routing", "tracing"):
        if flag in config:
            setting = _object(config[flag], f"{path}.{flag}")
            _fields(setting, {"enabled"}, f"{path}.{flag}")
            if "enabled" in setting and type(setting["enabled"]) is not bool:
                _invalid(f"{path}.{flag}.enabled", "expected a boolean")
    smart_routing = config.get("smart_routing", {}).get("enabled", False)
    if "models" in config:
        models = _object(config["models"], f"{path}.models")
        _fields(
            models,
            {"model_provider_service", "unity_catalog_location", "model_services"},
            f"{path}.models",
        )
        if "model_provider_service" in models:
            _string(models["model_provider_service"], f"{path}.models.model_provider_service")
        if "unity_catalog_location" in models:
            _fqn(models["unity_catalog_location"], f"{path}.models.unity_catalog_location", 2)
        static = models.get("model_services", [])
        if not isinstance(static, list):
            _invalid(f"{path}.models.model_services", "expected an array")
        for index, item in enumerate(static):
            _fqn(item, f"{path}.models.model_services[{index}]", 3)
        if len(static) > 100 or len(static) != len(set(static)):
            _invalid(f"{path}.models.model_services", "expected at most 100 unique models")
        sources = (
            int("model_provider_service" in models)
            + int("unity_catalog_location" in models)
            + bool(static)
        )
        if sources != 1:
            _invalid(f"{path}.models", "choose exactly one nonempty model source")
        for key, model in defaults.items():
            if static and model not in static:
                _invalid(f"{path}.default_models.{key}", "must appear in models.model_services")
        if smart_routing and (
            not static or any(not model.startswith("system.ai.") for model in static)
        ):
            _invalid(
                f"{path}.models.model_services", "smart routing requires static system.ai models"
            )


def validate_file_config(raw: object, agent: str) -> dict:
    """Reject unsupported content before the API normalizer can silently discard it."""
    config = _object(raw, "$")
    _fields(
        config,
        _METADATA
        | {
            "spec_version",
            "enabled_agents",
            "default_agent",
            "mcp_servers",
            "skills",
            "smart_defaults",
            "spend_tiers",
        },
        "$",
    )
    spec = config.get("spec_version")
    if type(spec) is not int or not 1 <= spec <= MAX_SPEC_VERSION:
        _invalid("$.spec_version", "expected a supported integer version (1)")
    for key in _METADATA & config.keys():
        value = config[key]
        if key in {"workspace_id", "created_user_id", "updated_user_id"}:
            if not (type(value) is int or (isinstance(value, str) and value.isdecimal())):
                _invalid(f"$.{key}", "expected an integer or decimal string")
        else:
            _string(value, f"$.{key}")
    entries = config.get("enabled_agents")
    if not isinstance(entries, list) or not entries:
        _invalid("$.enabled_agents", "expected a nonempty array")
    enabled = set()
    for index, entry in enumerate(entries):
        path = f"$.enabled_agents[{index}]"
        entry = _object(entry, path)
        _fields(entry, {"agent", "config"}, path)
        tool = _agent(entry.get("agent"), f"{path}.agent")
        if tool in enabled:
            _invalid(f"{path}.agent", "duplicate agent")
        enabled.add(tool)
        _validate_agent(entry.get("config"), f"{path}.config", tool)
    if agent not in enabled:
        _invalid("$.enabled_agents", "the requested agent must be enabled")
    if (
        "default_agent" in config
        and _agent(config["default_agent"], "$.default_agent") not in enabled
    ):
        _invalid("$.default_agent", "must appear in enabled_agents")
    for key in ("mcp_servers", "skills", "smart_defaults", "spend_tiers"):
        if key in config and _object(config[key], f"$.{key}"):
            _invalid(
                f"$.{key}", "nonempty selectors and budget policies are not supported in files"
            )
    normalized = normalize_managed_config(
        {key: value for key, value in config.items() if key not in _METADATA}
    )
    for entry in entries:
        if "custom_env" in entry["config"]:
            tool = _agent(entry["agent"], "$.enabled_agents.agent")
            normalized["enabled_agents"][tool]["custom_env"] = dict(entry["config"]["custom_env"])
    return normalized


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            _invalid("$", "duplicate JSON object key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    _invalid("$", "non-finite JSON numbers are not allowed")


def read_file_source(path: str, workspace: str, agent: str) -> SelectedManagedSource:
    """Read once per invocation without exposing input values in errors."""
    try:
        contents = Path(path).expanduser().read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise RuntimeError("Invalid --config-file at $: expected valid UTF-8 JSON.") from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError("Cannot read --config-file; supply a readable JSON file.") from exc
    try:
        raw = json.loads(
            contents, object_pairs_hook=_unique_object, parse_constant=_invalid_constant
        )
    except json.JSONDecodeError as exc:
        raise RuntimeError("Invalid --config-file at $: expected valid UTF-8 JSON.") from exc
    return SelectedManagedSource(workspace, agent, json.dumps(validate_file_config(raw, agent)))


def preflight_managed_resources(source: SelectedManagedSource, previous_state: dict) -> None:
    """Retain same-workspace UC assets; refuse a file launch that cannot migrate them."""
    from ucode.mcp import managed_mcp_servers
    from ucode.ui import print_note

    servers = managed_mcp_servers(previous_state, {source.agent})
    servers.extend(
        server
        for server in previous_state.get("mcp_servers", [])
        if source.agent in server.get("clients", [])
    )
    path = config_io.APP_DIR / "skills.json"
    try:
        records = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "Cannot inspect downloaded skills; repair ~/.ucode/skills.json before using --config-file."
        ) from exc
    downloads = records.get("skill_downloads", []) if isinstance(records, dict) else []
    skills = [
        record
        for record in downloads
        if isinstance(record, dict) and record.get("scope") == "managed"
    ]
    target_host = urlsplit(source.workspace).netloc
    foreign_servers = any(
        urlsplit(server.get("url") or server.get("workspace") or "").netloc != target_host
        for server in servers
    )
    foreign_skills = any(record.get("workspace") != source.workspace for record in skills)
    changing_workspace = (
        previous_state.get("workspace") and previous_state["workspace"] != source.workspace
    )
    if changing_workspace and (foreign_servers or foreign_skills):
        raise RuntimeError(
            "Previously ug-managed UC MCP servers or skills require migration before this workspace "
            "transition. Reconcile or remove those resources in their original workspace "
            "with `ug configure` / `ug revert`, then retry. Launch cannot clean them up."
        )
    if servers or skills:
        print_note("Retaining existing MCP servers and skills outside the config file.")
