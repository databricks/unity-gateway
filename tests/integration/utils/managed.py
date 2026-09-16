"""Shared builders and stub injection for the managed-config integration suites.

Pure CodingAgentConfig construction plus the ``UCODE_MANAGED_CONFIG_STUB`` filesystem/env mechanics.
The configure invocation, launch, and assertions stay visible in each test (tests/AGENTS.md), and
this module imports nothing from the ``ucode`` application package (tests/test_integration_contract.py
enforces that boundary).
"""

import json
from pathlib import Path


def set_managed_config_stub(session, tmp_path, config: dict | None) -> None:
    """Write ``config`` to a file and point ``UCODE_MANAGED_CONFIG_STUB`` at it for this session.

    ``None`` writes an explicit JSON ``null``, which reproduces a workspace that publishes no
    managed config (the stub's no-config state)."""
    stub = Path(tmp_path) / "managed-config.json"
    stub.write_text(json.dumps(config))
    session.env["UCODE_MANAGED_CONFIG_STUB"] = str(stub)


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
    models: list[str], *, family_defaults: dict[str, str] | None = None
) -> dict:
    default_models = {"default_model": models[0]}
    if family_defaults:
        default_models.update(
            {f"default_{family}_model": m for family, m in family_defaults.items()}
        )
    return {
        "agent": "CODING_AGENT_CLAUDE_CODE",
        "config": {
            "models": {"model_services": models},
            "default_models": default_models,
        },
    }


def build_codex_agent_config(*, models: list[str]) -> dict:
    return {
        "agent": "CODING_AGENT_CODEX",
        "config": {
            "models": {"model_services": models},
            "default_models": {"default_model": models[0]},
        },
    }


def build_mps_agent_config(agent: str, provider: str) -> dict:
    """A model-discovery config routing ``agent`` through a Model Provider Service (no static list)."""
    return {"agent": agent, "config": {"models": {"model_provider_service": provider}}}
