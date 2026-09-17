"""Shared builders and stub injection for the managed-config integration suites.

Pure CodingAgentConfig construction plus the ``UCODE_MANAGED_CONFIG_STUB`` filesystem/env mechanics.
The configure invocation, launch, and assertions stay visible in each test (tests/AGENTS.md), and
this module imports nothing from the ``ucode`` application package (tests/test_integration_contract.py
enforces that boundary).
"""

import json
from pathlib import Path


def set_managed_config_stub(session, tmp_path, config: dict) -> None:
    """Write ``config`` to a file and point ``UCODE_MANAGED_CONFIG_STUB`` at it for this session."""
    stub = Path(tmp_path) / "managed-config.json"
    stub.write_text(json.dumps(config))
    session.env["UCODE_MANAGED_CONFIG_STUB"] = str(stub)


def build_coding_agent_config(
    default_agent: str, *agents: dict, mcp_names: list[str] | None = None
) -> dict:
    config = {"spec_version": 1, "default_agent": default_agent, "enabled_agents": list(agents)}
    if mcp_names is not None:
        config["mcp_servers"] = {"names": mcp_names}
    return config


def build_claude_agent_config(models: list[str]) -> dict:
    return {
        "agent": "CODING_AGENT_CLAUDE_CODE",
        "config": {
            "models": {"model_services": models},
            "default_models": {"default_model": models[0]},
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
