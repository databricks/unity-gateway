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

_VERSIONS = {
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

# Only intentional agent differences belong here; omitted flags inherit the shared preset.
# Comment each override with the reason that agent differs.
_AGENT_OVERRIDES: dict[str, dict[str, dict[str, str]]] = {}


def _validate_versions(
    versions: Mapping[str, Mapping[str, str]],
    overrides: Mapping[str, Mapping[str, Mapping[str, str]]],
) -> None:
    """Require complete shared presets and valid, sparse agent overrides."""
    expected = set(SMART_ROUTING_ENV_KEYS)
    for version, agents in overrides.items():
        if version not in versions or agents.keys() - {AGENT_CLAUDE, AGENT_CODEX}:
            raise ValueError(
                f"Invalid smart-routing version {version!r}: unknown version or agent."
            )
    for version, shared in versions.items():
        configs = {"shared": shared}
        configs.update(
            {agent: {**shared, **values} for agent, values in overrides.get(version, {}).items()}
        )
        for agent, values in configs.items():
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


_validate_versions(_VERSIONS, _AGENT_OVERRIDES)


def _version_config(version: str, *, agent: str) -> dict[str, str] | None:
    shared = _VERSIONS.get(version)
    if shared is None or agent not in (AGENT_CLAUDE, AGENT_CODEX):
        return None
    return {**shared, **_AGENT_OVERRIDES.get(version, {}).get(agent, {})}


def resolve_environment(env: Mapping[str, str] | None = None, *, agent: str) -> dict[str, str]:
    """Expand a version before applying any launch or session-specific overrides."""
    resolved = dict(os.environ if env is None else env)
    version = resolved.pop(SMART_ROUTER_CONFIG_VERSION_ENV_VAR, "").strip()
    resolved.update(_version_config(version, agent=agent) or {})
    return resolved


def apply_config(
    env: MutableMapping[str, str] | None = None, *, agent: str
) -> dict[str, str | None]:
    """Consume the launch selector, returning the values needed to restore its input."""
    target = os.environ if env is None else env
    version = target.get(SMART_ROUTER_CONFIG_VERSION_ENV_VAR, "").strip()
    preset = _version_config(version, agent=agent)
    if preset is None:
        return {}
    keys = (*SMART_ROUTING_ENV_KEYS, SMART_ROUTER_CONFIG_VERSION_ENV_VAR)
    previous = {key: target.get(key) for key in keys}
    target.update(preset)
    target.pop(SMART_ROUTER_CONFIG_VERSION_ENV_VAR, None)
    return previous
