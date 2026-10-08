"""Exhaustive component coverage for versioned smart-routing configuration."""

from __future__ import annotations

import pytest

from ucode.constants import (
    ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR,
    ENABLE_SMART_ROUTING_ENV_VAR,
    ENABLE_SUBAGENT_ROUTING_ENV_VAR,
    SMART_ROUTING_CONFIG_VERSION_ENV_VAR,
)
from ucode.smart_routing import config

_EXPECTED_PRESETS = {
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


@pytest.mark.parametrize("v2_value", [None, "", "0", "1"])
@pytest.mark.parametrize("subagent_value", [None, "", "0", "1"])
@pytest.mark.parametrize("orchestrator_value", [None, "", "0", "1"])
@pytest.mark.parametrize(
    "config_version",
    [
        None,
        "",
        " \t",
        "subagent_only_v0",
        "subagent_only_v1",
        "subagent_orch_v0",
        " subagent_only_v0 ",
        " subagent_only_v1 ",
        " subagent_orch_v0 ",
        "subagent_only",
        "subagent_orch",
        "future_mode",
    ],
)
def test_smart_routing_config_cartesian_grid(
    v2_value, subagent_value, orchestrator_value, config_version
):
    source = {
        environment_key: value
        for environment_key, value in (
            (ENABLE_SMART_ROUTING_ENV_VAR, v2_value),
            (ENABLE_SUBAGENT_ROUTING_ENV_VAR, subagent_value),
            (ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR, orchestrator_value),
        )
        if value is not None
    }
    source["UNRELATED_SETTING"] = "preserved"
    if config_version is not None:
        source[SMART_ROUTING_CONFIG_VERSION_ENV_VAR] = config_version
    original = source.copy()
    normalized_version = (config_version or "").strip()

    if normalized_version and normalized_version not in _EXPECTED_PRESETS:
        with pytest.raises(RuntimeError):
            config.resolve_environment(source)
        assert source == original

        applied = source.copy()
        with pytest.raises(RuntimeError):
            config.apply_config(applied)
        assert applied == original
        return

    expected = original.copy()
    expected.pop(SMART_ROUTING_CONFIG_VERSION_ENV_VAR, None)
    if normalized_version:
        expected.update(_EXPECTED_PRESETS[normalized_version])

    resolved = config.resolve_environment(source)

    assert resolved == expected
    assert source == original

    applied = source.copy()
    config.apply_config(applied)

    expected_applied = expected if normalized_version else original
    assert applied == expected_applied
