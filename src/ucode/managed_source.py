"""Invocation-scoped managed policy selection and strict local wire validation.

The API cache belongs to ``managed_config``. Explicit files never read or write it.
Extension parsers are installed only by implementations that can apply their effects.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, NoReturn
from urllib.parse import urlsplit

from ucode import config_io
from ucode.managed_config import (
    AGENT_ENUM_TO_TOOL,
    AGENT_NAME_TO_TOOL,
    MAX_SPEC_VERSION,
    ManagedConfigResult,
    normalize_managed_config,
)

AgentExtensionParser = Callable[[object, str, str, dict], object]
SourceExtensionParser = Callable[[object, str], object]
AGENT_EXTENSION_PARSERS: dict[str, AgentExtensionParser] = {}
SOURCE_EXTENSION_PARSERS: dict[str, SourceExtensionParser] = {}
SUPPORTED_HANDOFF_TARGETS = {"user_settings", "private_settings", "managed_settings"}
_AGENT_EXTENSIONS = {"custom_env", "native_settings", "native_requirements"}
_METADATA = {
    "name",
    "workspace_id",
    "create_time",
    "created_user_id",
    "update_time",
    "updated_user_id",
    "retrieved_time",
}


@dataclass(frozen=True)
class SelectedManagedSource:
    """An immutable policy snapshot; callers receive independent normalized views.

    Path and content digest describe input provenance, never an ownership identity.
    Keeping the snapshot encoded prevents a consumer from changing another's policy.
    """

    kind: Literal["api", "file"]
    workspace: str
    agent: str
    resolved_path: Path | None = None
    digest: str | None = None
    feature_disabled: bool = False
    _manifest_json: str | None = field(default=None, repr=False)

    @property
    def manifest(self) -> dict | None:
        return json.loads(self._manifest_json) if self._manifest_json is not None else None

    @classmethod
    def from_api(
        cls, result: ManagedConfigResult, workspace: str, agent: str
    ) -> SelectedManagedSource:
        return cls(
            kind="api",
            workspace=workspace,
            agent=agent,
            feature_disabled=result[1],
            _manifest_json=json.dumps(result[0]) if result[0] is not None else None,
        )

    def check_target(self, workspace: str, agent: str | None = None) -> None:
        if self.workspace != workspace or (agent is not None and self.agent != agent):
            raise RuntimeError("Selected managed configuration does not match this launch target.")


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


def _handoff(value: object, path: str) -> dict:
    handoff = _object(value, path)
    _fields(handoff, {"schema_version", "owner", "migration_version", "agents"}, path)
    if type(handoff.get("schema_version")) is not int or handoff["schema_version"] != 1:
        _invalid(f"{path}.schema_version", "expected version 1")
    version = handoff.get("migration_version")
    if type(version) is not int or version < 1:
        _invalid(f"{path}.migration_version", "expected a positive integer")
    owner = _string(handoff.get("owner"), f"{path}.owner")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", owner) or owner == "workspace-api":
        _invalid(f"{path}.owner", "expected a stable non-reserved owner identifier")
    agents = _object(handoff.get("agents"), f"{path}.agents")
    for agent, declarations in agents.items():
        if agent not in {"claude", "codex"}:
            _invalid(f"{path}.agents.{agent}", "handoff supports claude and codex")
        location = f"{path}.agents.{agent}"
        declarations = _object(declarations, location)
        _fields(declarations, {"adopt", "retire"}, location)
        declared_paths = []
        for action, items in declarations.items():
            if not isinstance(items, list):
                _invalid(f"{location}.{action}", "expected an array")
            for index, item in enumerate(items):
                entry_path = f"{location}.{action}[{index}]"
                item = _object(item, entry_path)
                _fields(item, {"target", "path", "elements"}, entry_path)
                target = item.get("target")
                if not isinstance(target, str) or target not in SUPPORTED_HANDOFF_TARGETS:
                    _invalid(f"{entry_path}.target", "this build cannot apply this handoff target")
                parts = item.get("path")
                if (
                    not isinstance(parts, list)
                    or not parts
                    or any(
                        not isinstance(part, str) or not part or part.isdecimal() or "\x00" in part
                        for part in parts
                    )
                ):
                    _invalid(
                        f"{entry_path}.path", "expected nonempty field names without array indexes"
                    )
                if "elements" in item and not isinstance(item["elements"], list):
                    _invalid(
                        f"{entry_path}.elements", "expected an array of explicit contributions"
                    )
                for other_target, other_parts in declared_paths:
                    if target == other_target and (
                        parts == other_parts[: len(parts)]
                        or other_parts == parts[: len(other_parts)]
                    ):
                        _invalid(f"{entry_path}.path", "overlapping handoff declarations")
                declared_paths.append((target, parts))
    return handoff


SOURCE_EXTENSION_PARSERS["handoff"] = _handoff


def _fqn(value: object, path: str, parts: int) -> str:
    name = _string(value, path)
    if len(name.split(".")) != parts or not all(part.strip() for part in name.split(".")):
        _invalid(path, f"expected a {parts}-part Unity Catalog name")
    return name


def _extensions(
    value: dict, path: str, names: set[str], parsers: dict[str, SourceExtensionParser]
) -> dict:
    parsed = {}
    for name in names & value.keys():
        parser = parsers.get(name)
        if parser is None:
            _invalid(f"{path}.{name}", "this Unity Gateway build cannot apply this extension")
        parsed[name] = parser(value[name], f"{path}.{name}")
    return parsed


def _validate_agent(value: object, path: str, tool: str) -> dict:
    config = _object(value, path)
    _fields(
        config,
        {"models", "default_models", "http_headers", "smart_routing", "tracing"}
        | _AGENT_EXTENSIONS,
        path,
    )
    extensions = {}
    for name in _AGENT_EXTENSIONS & config.keys():
        parser = AGENT_EXTENSION_PARSERS.get(name)
        if parser is None:
            _invalid(f"{path}.{name}", "this Unity Gateway build cannot apply this extension")
        extensions[name] = parser(config[name], f"{path}.{name}", tool, config)
    defaults = _strings(config.get("default_models", {}), f"{path}.default_models")
    for key in defaults:
        if key != "default_model" and not (
            tool == "claude" and re.fullmatch(r"default_.+_model", key)
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
    if not smart_routing and "default_model" not in defaults:
        _invalid(f"{path}.default_models.default_model", "required unless smart routing is enabled")
    static: list[str] = []
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
        if "model_services" in models:
            static = models["model_services"]
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
        _invalid(f"{path}.models.model_services", "smart routing requires static system.ai models")
    return extensions


def validate_file_config(raw: object, agent: str) -> dict:
    """Validate the full published wire shape before its permissive API normalization."""
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
            "handoff",
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
    extras = _extensions(config, "$", {"handoff"}, SOURCE_EXTENSION_PARSERS)
    entries = config.get("enabled_agents")
    if not isinstance(entries, list) or not entries:
        _invalid("$.enabled_agents", "expected a nonempty array")
    enabled: dict[str, dict] = {}
    for index, entry in enumerate(entries):
        path = f"$.enabled_agents[{index}]"
        entry = _object(entry, path)
        _fields(entry, {"agent", "config"}, path)
        tool = _agent(entry.get("agent"), f"{path}.agent")
        if tool in enabled:
            _invalid(f"{path}.agent", "duplicate agent")
        enabled[tool] = _validate_agent(entry.get("config"), f"{path}.config", tool)
    if agent not in enabled:
        _invalid("$.enabled_agents", "the requested agent must be enabled")
    if (
        "default_agent" in config
        and _agent(config["default_agent"], "$.default_agent") not in enabled
    ):
        _invalid("$.default_agent", "must appear in enabled_agents")
    for key in ("mcp_servers", "skills", "smart_defaults", "spend_tiers"):
        if key in config:
            policy = _object(config[key], f"$.{key}")
            if policy:
                _invalid(
                    f"$.{key}", "nonempty selectors and budget policies are not supported in files"
                )
    # Published metadata, particularly update_time, cannot affect file freshness.
    manifest = normalize_managed_config(
        {key: value for key, value in config.items() if key not in _METADATA}
    )
    manifest.update(extras)
    for tool, extensions in enabled.items():
        manifest["enabled_agents"][tool].update(extensions)
    return manifest


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
    """Read exactly once, failing closed without exposing input values in errors."""
    try:
        resolved = Path(path).expanduser().resolve()
        contents = resolved.read_bytes()
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError("Cannot read --config-file; supply a readable JSON file.") from exc
    try:
        raw = json.loads(
            contents, object_pairs_hook=_unique_object, parse_constant=_invalid_constant
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Invalid --config-file at $: expected valid UTF-8 JSON.") from exc
    manifest = validate_file_config(raw, agent)
    return SelectedManagedSource(
        kind="file",
        workspace=workspace,
        agent=agent,
        resolved_path=resolved,
        digest=hashlib.sha256(contents).hexdigest(),
        _manifest_json=json.dumps(manifest),
    )


def preflight_managed_resources(source: SelectedManagedSource, previous_state: dict) -> None:
    """Retain same-workspace UC assets explicitly; refuse an unsupported migration."""
    from ucode.mcp import managed_mcp_servers
    from ucode.ui import print_note

    servers = managed_mcp_servers(previous_state, {source.agent})
    if source.kind == "file":
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
    if foreign_servers or foreign_skills:
        raise RuntimeError(
            "Previously ug-managed UC MCP servers or skills require migration before this workspace "
            "transition. Reconcile or remove those resources in their original workspace "
            "with `ug configure` / `ug revert`, then retry. Launch cannot clean them up."
        )
    if source.kind == "file" and (servers or skills):
        print_note(
            "Retaining same-workspace UC MCP servers and skills outside the config file's ownership."
        )
