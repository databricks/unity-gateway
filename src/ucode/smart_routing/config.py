"""Smart-routing versions, and the routing decision each launch makes from them."""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from enum import StrEnum

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

ROUTING_AGENTS = frozenset({AGENT_CLAUDE, AGENT_CODEX})

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


class RoutingSource(StrEnum):
    """The input that decided a launch's routing switches."""

    UNSUPPORTED_AGENT = "agent without smart routing"
    NATIVE_SUBCOMMAND = "native agent subcommand"
    CLI_DISABLE = "--disable-smart-routing"
    ENV_VERSION = SMART_ROUTER_CONFIG_VERSION_ENV_VAR
    CLI_ENABLE = "--enable-smart-routing"
    ADMIN_CONFIG = "admin smart_routing.enabled"
    ENV_SWITCHES = "routing env vars"


@dataclass(frozen=True)
class LaunchRouting:
    # The routing env vars the agent starts with; None leaves a var unset.
    switches: dict[str, str | None]
    source: RoutingSource
    # The switches route the first prompt or subagents.
    routing_on: bool
    # ug starts the agent through the smart-routing launcher.
    routed_launch: bool


def decide_launch_routing(
    *,
    agent: str,
    cli_flag: bool | None,
    env_version: str,
    env_switches: Mapping[str, str | None],
    admin_enabled: bool,
    native_subcommand: bool,
    pinned_model: bool,
) -> LaunchRouting:
    """Decide a launch's smart routing from every input. The first matching rule wins."""
    if agent not in ROUTING_AGENTS:
        return LaunchRouting(
            dict(env_switches),
            RoutingSource.UNSUPPORTED_AGENT,
            routing_on=False,
            routed_launch=False,
        )
    if native_subcommand:
        switches, source = dict.fromkeys(SMART_ROUTING_ENV_KEYS), RoutingSource.NATIVE_SUBCOMMAND
    elif cli_flag is False:
        switches, source = dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0"), RoutingSource.CLI_DISABLE
    elif env_version.strip() in _VERSIONS:
        switches, source = _VERSIONS[env_version.strip()][agent], RoutingSource.ENV_VERSION
    elif cli_flag:
        switches, source = _VERSIONS[DEFAULT_ENABLED_VERSION][agent], RoutingSource.CLI_ENABLE
    elif admin_enabled:
        switches, source = _VERSIONS[DEFAULT_ENABLED_VERSION][agent], RoutingSource.ADMIN_CONFIG
    else:
        switches, source = env_switches, RoutingSource.ENV_SWITCHES
    routing_on = "1" in (
        switches.get(ENABLE_SMART_ROUTING_ENV_VAR),
        switches.get(ENABLE_SUBAGENT_ROUTING_ENV_VAR),
    )
    # Only a developer's explicit model skips the routed launcher; a managed default model is the
    # starting model of a routed session.
    return LaunchRouting(
        dict(switches),
        source,
        routing_on=routing_on,
        routed_launch=routing_on and not pinned_model,
    )


def apply_launch_switches(
    switches: Mapping[str, str | None], env: MutableMapping[str, str] | None = None
) -> dict[str, str | None]:
    """Replace the selector with the launch's switches, returning the values needed to restore."""
    target = os.environ if env is None else env
    previous = {key: target.get(key) for key in _RESTORE_KEYS}
    target.pop(SMART_ROUTER_CONFIG_VERSION_ENV_VAR, None)
    for key, value in switches.items():
        if value is None:
            target.pop(key, None)
        else:
            target[key] = value
    return previous
