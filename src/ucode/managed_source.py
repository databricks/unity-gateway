"""Invocation-scoped local coding-agent configuration, separate from the API cache."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn
from urllib.parse import urlsplit

from ucode import config_io
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


def _agent(value: object, path: str) -> str:
    name = _string(value, path)
    tool = AGENT_ENUM_TO_TOOL.get(name) or AGENT_NAME_TO_TOOL.get(name)
    if tool is None:
        _invalid(path, "unknown agent")
    return tool


def validate_file_config(raw: object, agent: str | None = None) -> dict:
    """Treat the file as if it were a GET response: apply spec_version gate and normalize.

    When agent is None (for configure --config-file), validates that the file enables
    at least one known agent but does not check that a specific agent is enabled.
    When agent is specified (for launch --config-file), validates that the requested
    agent is enabled.
    """
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
    # Apply the same spec_version gate as the fetch path (_gate_config).
    spec = config.get("spec_version")
    if spec is not None:
        if isinstance(spec, bool) or not isinstance(spec, int):
            _invalid("$.spec_version", "expected a supported integer version (1)")
        if spec > MAX_SPEC_VERSION:
            _invalid(
                "$.spec_version",
                f"Your managed configuration needs a newer Unity Gateway (spec_version {spec}; "
                f"this build supports up to {MAX_SPEC_VERSION}). Run `ug upgrade`.",
            )

    # Minimal shape check: enabled_agents must be nonempty (so we have a target to verify).
    entries = config.get("enabled_agents")
    if not isinstance(entries, list) or not entries:
        _invalid("$.enabled_agents", "expected a nonempty array")

    # Verify each agent entry has the basic structure and collect enabled agents.
    enabled = set()
    for index, entry in enumerate(entries):
        path = f"$.enabled_agents[{index}]"
        entry_obj = _object(entry, path)
        _fields(entry_obj, {"agent", "config"}, path)
        tool = _agent(entry_obj.get("agent"), f"{path}.agent")
        if tool in enabled:
            _invalid(f"{path}.agent", "duplicate agent")
        enabled.add(tool)

    # For launch path: verify the requested agent is enabled.
    # For configure path (agent=None): just verify at least one agent is enabled.
    if agent is not None and agent not in enabled:
        _invalid("$.enabled_agents", "the requested agent must be enabled")
    if not enabled:
        _invalid("$.enabled_agents", "the file must enable at least one known agent")

    # Strip metadata and normalize, mirroring the fetch path (managed_config.get_managed_config).
    return normalize_managed_config(
        {key: value for key, value in config.items() if key not in _METADATA}
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            _invalid("$", "duplicate JSON object key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    _invalid("$", "non-finite JSON numbers are not allowed")


def load_file_config_manifest(path: str) -> dict:
    """Read and validate a config file for configure --config-file, without agent binding.

    Returns the normalized manifest for the entire file, allowing the caller to check
    which agents are enabled and select one if needed.
    """
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
    return validate_file_config(raw, agent=None)


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
