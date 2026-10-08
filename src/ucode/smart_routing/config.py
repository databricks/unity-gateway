"""Resolve external smart-routing versions into backward-compatible feature flags."""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping

from ucode.constants import (
    ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR,
    ENABLE_SMART_ROUTING_ENV_VAR,
    ENABLE_SUBAGENT_ROUTING_ENV_VAR,
    SMART_ROUTER_CONFIG_VERSION_ENV_VAR,
    SMART_ROUTING_ENV_KEYS,
)

_VERSIONS = {
    "first_prompt_and_subagent_no_orch_v0": {
        ENABLE_SMART_ROUTING_ENV_VAR: "1",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "0",
    },
    "subagent_only_v0": {
        ENABLE_SMART_ROUTING_ENV_VAR: "0",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "0",
    },
    "subagent_only_v1": {
        ENABLE_SMART_ROUTING_ENV_VAR: "1",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "0",
    },
    "subagent_orch_v0": {
        ENABLE_SMART_ROUTING_ENV_VAR: "0",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "1",
    },
}


def _validate_versions(versions: Mapping[str, Mapping[str, str]]) -> None:
    """Require every version to explicitly configure the complete managed flag set."""
    expected = set(SMART_ROUTING_ENV_KEYS)
    for version, values in versions.items():
        missing = expected - values.keys()
        unexpected = values.keys() - expected
        if missing or unexpected:
            raise ValueError(
                f"Invalid smart-routing version {version!r}: "
                f"missing env vars {sorted(missing)}; unexpected env vars {sorted(unexpected)}."
            )
        for key, value in values.items():
            if value not in ("0", "1"):
                raise ValueError(
                    f"Invalid smart-routing version {version!r}: "
                    f"{key} must be '0' or '1', got {value!r}."
                )


_validate_versions(_VERSIONS)


def resolve_environment(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Expand a version before applying any launch or session-specific overrides."""
    resolved = dict(os.environ if env is None else env)
    version = resolved.pop(SMART_ROUTER_CONFIG_VERSION_ENV_VAR, "").strip()
    if not version:
        return resolved
    if version not in _VERSIONS:
        raise RuntimeError(
            f"Unknown {SMART_ROUTER_CONFIG_VERSION_ENV_VAR} value {version!r}. "
            f"Use one of: {', '.join(_VERSIONS)}, or unset it to use the legacy flags."
        )
    resolved.update(_VERSIONS[version])
    return resolved


def apply_config(env: MutableMapping[str, str] | None = None) -> dict[str, str | None]:
    """Consume the launch selector, returning the values needed to restore its input."""
    target = os.environ if env is None else env
    version = target.get(SMART_ROUTER_CONFIG_VERSION_ENV_VAR, "").strip()
    if not version:
        return {}
    resolved = resolve_environment(target)
    keys = (*SMART_ROUTING_ENV_KEYS, SMART_ROUTER_CONFIG_VERSION_ENV_VAR)
    previous = {key: target.get(key) for key in keys}
    target.update({key: resolved[key] for key in SMART_ROUTING_ENV_KEYS})
    target.pop(SMART_ROUTER_CONFIG_VERSION_ENV_VAR, None)
    return previous
