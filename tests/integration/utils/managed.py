"""Shared fetching, builders, and stub injection for managed-config integration suites.

Pure CodingAgentConfig construction plus the ``UCODE_MANAGED_CONFIG_STUB`` filesystem/env mechanics.
The configure invocation, launch, and assertions stay visible in each test (tests/AGENTS.md), and
this module imports nothing from the ``ucode`` application package (tests/test_integration_contract.py
enforces that boundary).
"""

import json
import urllib.request
from pathlib import Path

MANAGED_CONFIGS_PATH = "/api/ai-gateway/v2/coding-agent-configs"


def assert_no_managed_config(payload: object) -> None:
    """Validate the real List response before claiming unmanaged-workspace coverage."""
    configs = payload.get("coding_agent_configs", []) if isinstance(payload, dict) else payload
    assert isinstance(configs, list) and all(isinstance(config, dict) for config in configs), (
        "Invalid CodingAgentConfig listing; cannot establish an unmanaged workspace"
    )
    names = [config.get("name", "<unnamed>") for config in configs]
    assert not configs, (
        "Unmanaged discovery requires a workspace with no CodingAgentConfig; "
        f"the selected workspace publishes {names}. Use an unmanaged workspace for these "
        "cases. The suite will not remove or bypass shared admin configuration."
    )


def fetch_managed_config_stub(
    workspace: str,
    token: str,
    directory: Path,
    filename: str,
    *,
    agent: str,
    provider_service: str,
) -> Path:
    """Fetch the workspace config and persist one agent's MPS-backed test variant."""
    request = urllib.request.Request(
        workspace.rstrip("/") + MANAGED_CONFIGS_PATH,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)

    if isinstance(payload, dict):
        configs = payload.get("coding_agent_configs")
    elif isinstance(payload, list):
        configs = payload
    else:
        configs = None
    assert isinstance(configs, list) and configs, "workspace returned no managed CodingAgentConfig"
    config = json.loads(json.dumps(configs[0]))
    assert isinstance(config, dict), "managed CodingAgentConfig was not an object"

    enabled_agents = config.get("enabled_agents")
    assert isinstance(enabled_agents, list), "managed CodingAgentConfig had no enabled_agents list"
    matching_agents = [
        entry for entry in enabled_agents if isinstance(entry, dict) and entry.get("agent") == agent
    ]
    assert len(matching_agents) == 1, f"expected exactly one {agent} entry"
    agent_config = matching_agents[0].get("config")
    assert isinstance(agent_config, dict), f"{agent} had no managed agent config"

    agent_config["models"] = {"model_provider_service": provider_service}
    agent_config.pop("default_models", None)

    stub = directory / filename
    stub.write_text(json.dumps(config), encoding="utf-8")
    stub.chmod(0o600)
    return stub


def use_managed_config_stub(session, stub: Path) -> None:
    """Point one isolated session at an already-persisted raw CodingAgentConfig."""
    session.env["UCODE_MANAGED_CONFIG_STUB"] = str(stub)


def set_managed_config_stub(session, tmp_path, config: dict | None) -> None:
    """Write ``config`` to a file and point ``UCODE_MANAGED_CONFIG_STUB`` at it for this session.

    ``None`` writes an explicit JSON ``null``, which reproduces a workspace that publishes no
    managed config (the stub's no-config state)."""
    stub = Path(tmp_path) / "managed-config.json"
    stub.write_text(json.dumps(config))
    use_managed_config_stub(session, stub)


def is_managed_config_control_plane_cache(home: Path, path: Path) -> bool:
    """Whether ``path`` is ug's expected fetched-config cache, not agent-owned state."""
    return path == home / ".ucode" / "managed-config.json"


def build_coding_agent_config(
    default_agent: str,
    *agents: dict,
    mcp_names: list[str] | None = None,
    skill_names: list[str] | None = None,
    skills_location: str | None = None,
) -> dict:
    config = {"spec_version": 1, "default_agent": default_agent, "enabled_agents": list(agents)}
    if mcp_names is not None:
        config["mcp_servers"] = {"names": mcp_names}
    if skill_names is not None:
        config["skills"] = {"names": skill_names}
    elif skills_location is not None:
        config["skills"] = {"unity_catalog_location": skills_location}
    return config


def build_claude_agent_config(
    models: list[str],
    *,
    family_defaults: dict[str, str] | None = None,
    smart_routing: bool = False,
    otel_tracing_enabled: bool | None = None,
) -> dict:
    default_models = {"default_model": models[0]}
    if family_defaults:
        default_models.update(
            {f"default_{family}_model": m for family, m in family_defaults.items()}
        )
    config = {
        "models": {"model_services": models},
        "default_models": default_models,
    }
    if smart_routing:
        config["smart_routing"] = {"enabled": True}
    if otel_tracing_enabled is not None:
        config["tracing"] = {"enabled": otel_tracing_enabled}
    return {"agent": "CODING_AGENT_CLAUDE_CODE", "config": config}


def build_codex_agent_config(
    *,
    models: list[str],
    smart_routing: bool = False,
    http_headers: dict[str, str] | None = None,
    otel_tracing_enabled: bool | None = None,
) -> dict:
    config = {
        "models": {"model_services": models},
        "default_models": {"default_model": models[0]},
    }
    if smart_routing:
        config["smart_routing"] = {"enabled": True}
    if http_headers is not None:
        config["http_headers"] = http_headers
    if otel_tracing_enabled is not None:
        config["tracing"] = {"enabled": otel_tracing_enabled}
    return {"agent": "CODING_AGENT_CODEX", "config": config}


def build_mps_agent_config(agent: str, provider: str) -> dict:
    """A model-discovery config routing ``agent`` through a Model Provider Service (no static list)."""
    return {"agent": agent, "config": {"models": {"model_provider_service": provider}}}
