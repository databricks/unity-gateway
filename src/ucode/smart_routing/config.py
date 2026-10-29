"""Resolve external smart-routing versions into backward-compatible feature flags."""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping

from ucode.constants import (
    AGENT_CLAUDE,
    AGENT_CODEX,
    ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR,
    ENABLE_SMART_ROUTING_ENV_VAR,
    ENABLE_SUBAGENT_ROUTING_ENV_VAR,
    SMART_ROUTER_CONFIG_VERSION_ENV_VAR,
    SMART_ROUTING_ENV_KEYS,
)

# Customer preset: route the first prompt and subagents, without orchestration.
FIRST_PROMPT_AND_SUBAGENT_NO_ORCH_V0 = "first_prompt_and_subagent_no_orch_v0"

# Route only subagents, with V2 disabled and no orchestration.
SUBAGENT_ONLY_V0 = "subagent_only_v0"

# Route only subagents, with both V2 and subagent-only flags enabled; no orchestration.
SUBAGENT_ONLY_V1 = "subagent_only_v1"

# Route only subagents and inject the Smart Router Orchestrator workflow.
SUBAGENT_ORCH_V0 = "subagent_orch_v0"

# Route only subagents with V2, subagent-only, and orchestration all enabled.
SUBAGENT_ORCH_V1 = "subagent_orch_v1"

# Preserve subagent_orch_v0 for Claude, with all Codex routing flags disabled.
SUBAGENT_ORCH_V0_CLAUDE_ONLY = "subagent_orch_v0_claude_only"

# Selected by --enable-smart-routing and managed smart routing when no known version is set.
DEFAULT_ENABLED_VERSION = SUBAGENT_ORCH_V0

_RESTORE_KEYS = (*SMART_ROUTING_ENV_KEYS, SMART_ROUTER_CONFIG_VERSION_ENV_VAR)

_BASE_VERSIONS = {
    FIRST_PROMPT_AND_SUBAGENT_NO_ORCH_V0: {
        ENABLE_SMART_ROUTING_ENV_VAR: "1",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "0",
    },
    SUBAGENT_ONLY_V0: {
        ENABLE_SMART_ROUTING_ENV_VAR: "0",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "0",
    },
    SUBAGENT_ONLY_V1: {
        ENABLE_SMART_ROUTING_ENV_VAR: "1",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "0",
    },
    SUBAGENT_ORCH_V0: {
        ENABLE_SMART_ROUTING_ENV_VAR: "0",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "1",
    },
    SUBAGENT_ORCH_V1: {
        ENABLE_SMART_ROUTING_ENV_VAR: "1",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "1",
    },
}

# Agents share the base preset unless explicitly changed; comment any intentional difference.
_VERSIONS = {
    FIRST_PROMPT_AND_SUBAGENT_NO_ORCH_V0: {
        AGENT_CLAUDE: _BASE_VERSIONS[FIRST_PROMPT_AND_SUBAGENT_NO_ORCH_V0],
        AGENT_CODEX: _BASE_VERSIONS[FIRST_PROMPT_AND_SUBAGENT_NO_ORCH_V0],
    },
    SUBAGENT_ONLY_V0: {
        AGENT_CLAUDE: _BASE_VERSIONS[SUBAGENT_ONLY_V0],
        AGENT_CODEX: _BASE_VERSIONS[SUBAGENT_ONLY_V0],
    },
    SUBAGENT_ONLY_V1: {
        AGENT_CLAUDE: _BASE_VERSIONS[SUBAGENT_ONLY_V1],
        AGENT_CODEX: _BASE_VERSIONS[SUBAGENT_ONLY_V1],
    },
    SUBAGENT_ORCH_V0: {
        AGENT_CLAUDE: _BASE_VERSIONS[SUBAGENT_ORCH_V0],
        AGENT_CODEX: _BASE_VERSIONS[SUBAGENT_ORCH_V0],
    },
    SUBAGENT_ORCH_V1: {
        AGENT_CLAUDE: _BASE_VERSIONS[SUBAGENT_ORCH_V1],
        AGENT_CODEX: _BASE_VERSIONS[SUBAGENT_ORCH_V1],
    },
    # Codex opts out of routing and orchestration; Claude keeps subagent_orch_v0.
    SUBAGENT_ORCH_V0_CLAUDE_ONLY: {
        AGENT_CLAUDE: _BASE_VERSIONS[SUBAGENT_ORCH_V0],
        AGENT_CODEX: {
            ENABLE_SMART_ROUTING_ENV_VAR: "0",
            ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0",
            ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "0",
        },
    },
}


def _validate_versions(versions: Mapping[str, Mapping[str, Mapping[str, str]]]) -> None:
    """Require both agents and a complete, explicit flag set for each."""
    expected_agents = {AGENT_CLAUDE, AGENT_CODEX}
    expected = set(SMART_ROUTING_ENV_KEYS)
    for version, agents in versions.items():
        if agents.keys() != expected_agents:
            raise ValueError(
                f"Invalid smart-routing version {version!r}: "
                f"expected agents {sorted(expected_agents)}, got {sorted(agents)}."
            )
        for agent, values in agents.items():
            missing = expected - values.keys()
            unexpected = values.keys() - expected
            if missing or unexpected:
                raise ValueError(
                    f"Invalid smart-routing version {version!r} for {agent!r}: "
                    f"missing env vars {sorted(missing)}; unexpected env vars {sorted(unexpected)}."
                )
            for key, value in values.items():
                if value not in ("0", "1"):
                    raise ValueError(
                        f"Invalid smart-routing version {version!r} for {agent!r}: "
                        f"{key} must be '0' or '1', got {value!r}."
                    )


_validate_versions(_VERSIONS)


def resolve_environment(env: Mapping[str, str] | None = None, *, agent: str) -> dict[str, str]:
    """Expand a version before applying any launch or session-specific overrides."""
    resolved = dict(os.environ if env is None else env)
    version = resolved.pop(SMART_ROUTER_CONFIG_VERSION_ENV_VAR, "").strip()
    resolved.update(_VERSIONS.get(version, {}).get(agent, {}))
    return resolved


def apply_config(
    env: MutableMapping[str, str] | None = None, *, agent: str
) -> dict[str, str | None]:
    """Consume the launch selector, returning the values needed to restore its input."""
    target = os.environ if env is None else env
    version = target.get(SMART_ROUTER_CONFIG_VERSION_ENV_VAR, "").strip()
    preset = _VERSIONS.get(version, {}).get(agent)
    if preset is None:
        return {}
    previous = {key: target.get(key) for key in _RESTORE_KEYS}
    target.update(preset)
    target.pop(SMART_ROUTER_CONFIG_VERSION_ENV_VAR, None)
    return previous


def known_version(env: Mapping[str, str] | None = None) -> bool:
    """Whether the launch selector names a version, which then decides the launch's routing."""
    source = os.environ if env is None else env
    return source.get(SMART_ROUTER_CONFIG_VERSION_ENV_VAR, "").strip() in _VERSIONS


def apply_enabled_version(
    env: MutableMapping[str, str] | None = None, *, agent: str
) -> dict[str, str | None]:
    """Turn routing on through a version, returning the values needed to restore the input.

    A known version is kept; otherwise the selector is set to ``DEFAULT_ENABLED_VERSION``. Either
    way the version then overwrites every routing env var.
    """
    target = os.environ if env is None else env
    previous = {key: target.get(key) for key in _RESTORE_KEYS}
    if not known_version(target):
        target[SMART_ROUTER_CONFIG_VERSION_ENV_VAR] = DEFAULT_ENABLED_VERSION
    apply_config(target, agent=agent)
    return previous
