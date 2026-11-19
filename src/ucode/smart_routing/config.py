"""Resolve external smart-routing versions into backward-compatible feature flags."""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar

from ucode.constants import (
    AGENT_CLAUDE,
    AGENT_CODEX,
    AGENT_COPILOT,
    ENABLE_SMART_ROUTER_AUTO_MODE_ENV_VAR,
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

SUBAGENT_AUTO_V0_CLAUDE_ONLY = "subagent_auto_v0_claude_only"

# Opt-in Copilot subagent routing; Claude retains its default orchestration and Codex stays off.
COPILOT_SUBAGENT_ONLY_V0 = "copilot_subagent_only_v0"

# Enable Copilot's launch-scoped orchestration context as well as child routing.
COPILOT_SUBAGENT_ORCH_V0 = "copilot_subagent_orch_v0"

# Shared enable/managed default; the explicit Copilot flag selects its own opt-in preset.
DEFAULT_ENABLED_VERSION = SUBAGENT_ORCH_V0_CLAUDE_ONLY
_EXPLICIT_ENABLE_DEFAULTS = {AGENT_COPILOT: COPILOT_SUBAGENT_ORCH_V0}

_RESTORE_KEYS = (*SMART_ROUTING_ENV_KEYS, SMART_ROUTER_CONFIG_VERSION_ENV_VAR)

_BASE_VERSIONS = {
    FIRST_PROMPT_AND_SUBAGENT_NO_ORCH_V0: {
        ENABLE_SMART_ROUTING_ENV_VAR: "1",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "0",
        ENABLE_SMART_ROUTER_AUTO_MODE_ENV_VAR: "0",
    },
    SUBAGENT_ONLY_V0: {
        ENABLE_SMART_ROUTING_ENV_VAR: "0",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "0",
        ENABLE_SMART_ROUTER_AUTO_MODE_ENV_VAR: "0",
    },
    SUBAGENT_ONLY_V1: {
        ENABLE_SMART_ROUTING_ENV_VAR: "1",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "0",
        ENABLE_SMART_ROUTER_AUTO_MODE_ENV_VAR: "0",
    },
    SUBAGENT_ORCH_V0: {
        ENABLE_SMART_ROUTING_ENV_VAR: "0",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "1",
        ENABLE_SMART_ROUTER_AUTO_MODE_ENV_VAR: "0",
    },
    SUBAGENT_ORCH_V1: {
        ENABLE_SMART_ROUTING_ENV_VAR: "1",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "1",
        ENABLE_SMART_ROUTER_AUTO_MODE_ENV_VAR: "0",
    },
}

_ALL_OFF = {
    ENABLE_SMART_ROUTING_ENV_VAR: "0",
    ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0",
    ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "0",
    ENABLE_SMART_ROUTER_AUTO_MODE_ENV_VAR: "0",
}


# Agents share the base preset unless explicitly changed; comment any intentional difference.
_VERSIONS = {
    FIRST_PROMPT_AND_SUBAGENT_NO_ORCH_V0: {
        AGENT_CLAUDE: _BASE_VERSIONS[FIRST_PROMPT_AND_SUBAGENT_NO_ORCH_V0],
        AGENT_CODEX: _BASE_VERSIONS[FIRST_PROMPT_AND_SUBAGENT_NO_ORCH_V0],
        AGENT_COPILOT: _ALL_OFF,
    },
    SUBAGENT_ONLY_V0: {
        AGENT_CLAUDE: _BASE_VERSIONS[SUBAGENT_ONLY_V0],
        AGENT_CODEX: _BASE_VERSIONS[SUBAGENT_ONLY_V0],
        AGENT_COPILOT: _ALL_OFF,
    },
    SUBAGENT_ONLY_V1: {
        AGENT_CLAUDE: _BASE_VERSIONS[SUBAGENT_ONLY_V1],
        AGENT_CODEX: _BASE_VERSIONS[SUBAGENT_ONLY_V1],
        AGENT_COPILOT: _ALL_OFF,
    },
    SUBAGENT_ORCH_V0: {
        AGENT_CLAUDE: _BASE_VERSIONS[SUBAGENT_ORCH_V0],
        AGENT_CODEX: _BASE_VERSIONS[SUBAGENT_ORCH_V0],
        AGENT_COPILOT: _ALL_OFF,
    },
    SUBAGENT_ORCH_V1: {
        AGENT_CLAUDE: _BASE_VERSIONS[SUBAGENT_ORCH_V1],
        AGENT_CODEX: _BASE_VERSIONS[SUBAGENT_ORCH_V1],
        AGENT_COPILOT: _ALL_OFF,
    },
    # Codex and Copilot opt out of routing and orchestration; Claude keeps subagent_orch_v0.
    SUBAGENT_ORCH_V0_CLAUDE_ONLY: {
        AGENT_CLAUDE: _BASE_VERSIONS[SUBAGENT_ORCH_V0],
        AGENT_CODEX: _ALL_OFF,
        AGENT_COPILOT: _ALL_OFF,
    },
    # Codex opts out; Claude keeps subagent_orch_v0 and Copilot routes subagents.
    COPILOT_SUBAGENT_ONLY_V0: {
        AGENT_CLAUDE: _BASE_VERSIONS[SUBAGENT_ORCH_V0],
        AGENT_CODEX: _ALL_OFF,
        AGENT_COPILOT: _BASE_VERSIONS[SUBAGENT_ONLY_V0],
    },
    COPILOT_SUBAGENT_ORCH_V0: {
        AGENT_CLAUDE: _BASE_VERSIONS[SUBAGENT_ORCH_V0],
        AGENT_CODEX: _ALL_OFF,
        AGENT_COPILOT: _BASE_VERSIONS[SUBAGENT_ORCH_V0],
    },
    SUBAGENT_AUTO_V0_CLAUDE_ONLY: {
        AGENT_CLAUDE: {
            **_BASE_VERSIONS[SUBAGENT_ORCH_V0],
            ENABLE_SMART_ROUTER_AUTO_MODE_ENV_VAR: "1",
        },
        AGENT_CODEX: _ALL_OFF,
        AGENT_COPILOT: _ALL_OFF,
    },
}


def _validate_versions(versions: Mapping[str, Mapping[str, Mapping[str, str]]]) -> None:
    """Require every supported agent and a complete, explicit flag set for each."""
    expected_agents = {AGENT_CLAUDE, AGENT_CODEX, AGENT_COPILOT}
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


_selected_version: ContextVar[str] = ContextVar("smart_router_config_version", default="")


def selected_version(env: Mapping[str, str] | None = None) -> str:
    """Read a selector or the current launch's already-consumed selector."""
    source = os.environ if env is None else env
    if env is not None or SMART_ROUTER_CONFIG_VERSION_ENV_VAR in source:
        return source.get(SMART_ROUTER_CONFIG_VERSION_ENV_VAR, "").strip()
    return _selected_version.get()


@contextmanager
def version_scope() -> Iterator[None]:
    """Retain version identity for this call without reapplying its routing flags."""
    token = _selected_version.set(selected_version())
    try:
        yield
    finally:
        _selected_version.reset(token)


def auto_mode_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether this launch selected Claude Auto's configuration version."""
    return selected_version(env) == SUBAGENT_AUTO_V0_CLAUDE_ONLY


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


def _enabled_version(env: Mapping[str, str], *, agent: str, managed: bool) -> str:
    version = env.get(SMART_ROUTER_CONFIG_VERSION_ENV_VAR, "").strip()
    if version in _VERSIONS:
        return version
    return (
        DEFAULT_ENABLED_VERSION
        if managed
        else _EXPLICIT_ENABLE_DEFAULTS.get(agent, DEFAULT_ENABLED_VERSION)
    )


def enabled_version_routes(
    env: Mapping[str, str] | None = None, *, agent: str, managed: bool = False
) -> bool:
    """Whether the version ``apply_enabled_version`` would select routes this agent at all.

    Orchestration alone does not route, so only the V2 and subagent-only flags count. An agent
    the versions do not cover is never routed.
    """
    source = os.environ if env is None else env
    preset = _VERSIONS[_enabled_version(source, agent=agent, managed=managed)].get(agent)
    if preset is None:
        return False
    return "1" in (preset[ENABLE_SMART_ROUTING_ENV_VAR], preset[ENABLE_SUBAGENT_ROUTING_ENV_VAR])


def apply_enabled_version(
    env: MutableMapping[str, str] | None = None, *, agent: str, managed: bool = False
) -> dict[str, str | None]:
    """Turn routing on through a version, returning the values needed to restore the input.

    Keep a known version. Otherwise explicit Copilot enable opts into its orchestration preset;
    managed enable and other agents retain the shared default. Restore the full input on exit.
    """
    target = os.environ if env is None else env
    previous = {key: target.get(key) for key in _RESTORE_KEYS}
    target[SMART_ROUTER_CONFIG_VERSION_ENV_VAR] = _enabled_version(
        target, agent=agent, managed=managed
    )
    apply_config(target, agent=agent)
    return previous
