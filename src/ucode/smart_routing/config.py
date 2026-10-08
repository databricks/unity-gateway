"""Resolve external smart-routing versions into backward-compatible feature flags."""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping

from ucode.constants import (
    ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR,
    ENABLE_SMART_ROUTING_ENV_VAR,
    ENABLE_SUBAGENT_ROUTING_ENV_VAR,
    SMART_ROUTING_CONFIG_VERSION_ENV_VAR,
)

_VERSIONS = {
    "subagent_only": {
        ENABLE_SMART_ROUTING_ENV_VAR: "0",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "0",
    },
    "subagent_orch": {
        ENABLE_SMART_ROUTING_ENV_VAR: "0",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "1",
    },
}


def resolve_environment(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Expand a version before applying any launch or session-specific overrides."""
    resolved = dict(os.environ if env is None else env)
    version = resolved.pop(SMART_ROUTING_CONFIG_VERSION_ENV_VAR, "").strip()
    if not version:
        return resolved
    if version not in _VERSIONS:
        raise RuntimeError(
            f"Unknown {SMART_ROUTING_CONFIG_VERSION_ENV_VAR} value {version!r}. "
            f"Use one of: {', '.join(_VERSIONS)}, or unset it to use the legacy flags."
        )
    resolved.update(_VERSIONS[version])
    return resolved


def apply_config(env: MutableMapping[str, str] | None = None) -> dict[str, str | None]:
    """Consume the launch selector, returning the values needed to restore its input."""
    target = os.environ if env is None else env
    version = target.get(SMART_ROUTING_CONFIG_VERSION_ENV_VAR, "").strip()
    if not version:
        return {}
    resolved = resolve_environment(target)
    keys = (*_VERSIONS[version], SMART_ROUTING_CONFIG_VERSION_ENV_VAR)
    previous = {key: target.get(key) for key in keys}
    target.update({key: resolved[key] for key in _VERSIONS[version]})
    target.pop(SMART_ROUTING_CONFIG_VERSION_ENV_VAR, None)
    return previous
