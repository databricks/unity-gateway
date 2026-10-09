"""Component coverage for supported versioned smart-routing configuration."""

from __future__ import annotations

import pytest

from ucode.constants import (
    ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR,
    ENABLE_SMART_ROUTING_ENV_VAR,
    ENABLE_SUBAGENT_ROUTING_ENV_VAR,
    SMART_ROUTER_CONFIG_VERSION_ENV_VAR,
)
from ucode.smart_routing import config, orchestrator, v2

_EXPECTED_PRESETS = {
    config.FIRST_PROMPT_AND_SUBAGENT_NO_ORCH_V0: {
        ENABLE_SMART_ROUTING_ENV_VAR: "1",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "0",
    },
    config.SUBAGENT_ONLY_V0: {
        ENABLE_SMART_ROUTING_ENV_VAR: "0",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "0",
    },
    config.SUBAGENT_ONLY_V1: {
        ENABLE_SMART_ROUTING_ENV_VAR: "1",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "0",
    },
    config.SUBAGENT_ORCH_V0: {
        ENABLE_SMART_ROUTING_ENV_VAR: "0",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "1",
    },
    config.SUBAGENT_ORCH_V1: {
        ENABLE_SMART_ROUTING_ENV_VAR: "1",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "1",
    },
}


@pytest.mark.parametrize(
    "selector", [None, "", "   ", "unsupported_version", " unsupported_version "]
)
def test_absent_or_unknown_selector_is_a_no_op(selector):
    applied = {
        ENABLE_SMART_ROUTING_ENV_VAR: "1",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "1",
        "INHERITED_SETTING": "preserved",
    }
    expected = applied.copy()
    if selector is not None:
        applied[SMART_ROUTER_CONFIG_VERSION_ENV_VAR] = selector
    original = applied.copy()

    assert config.resolve_environment(applied) == expected
    assert applied == original
    assert config.apply_config(applied) == {}
    assert applied == original


@pytest.mark.parametrize("ENABLE_SMART_ROUTING_V2", [None, "0", "1"])
@pytest.mark.parametrize("ENABLE_SMART_ROUTING_SUBAGENT_ONLY", [None, "0", "1"])
@pytest.mark.parametrize("ENABLE_SMART_ROUTER_ORCHESTRATOR", [None, "0", "1"])
@pytest.mark.parametrize(
    "SMART_ROUTER_CONFIG_VERSION",
    [
        None,
        config.FIRST_PROMPT_AND_SUBAGENT_NO_ORCH_V0,
        config.SUBAGENT_ONLY_V0,
        config.SUBAGENT_ONLY_V1,
        config.SUBAGENT_ORCH_V0,
        config.SUBAGENT_ORCH_V1,
    ],
)
def test_smart_routing_config_cartesian_grid(
    ENABLE_SMART_ROUTING_V2,
    ENABLE_SMART_ROUTING_SUBAGENT_ONLY,
    ENABLE_SMART_ROUTER_ORCHESTRATOR,
    SMART_ROUTER_CONFIG_VERSION,
):
    source = {
        environment_key: value
        for environment_key, value in (
            (ENABLE_SMART_ROUTING_ENV_VAR, ENABLE_SMART_ROUTING_V2),
            (ENABLE_SUBAGENT_ROUTING_ENV_VAR, ENABLE_SMART_ROUTING_SUBAGENT_ONLY),
            (ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR, ENABLE_SMART_ROUTER_ORCHESTRATOR),
        )
        if value is not None
    }
    source["UNRELATED_SETTING"] = "preserved"
    if SMART_ROUTER_CONFIG_VERSION is not None:
        source[SMART_ROUTER_CONFIG_VERSION_ENV_VAR] = SMART_ROUTER_CONFIG_VERSION
    original = source.copy()

    expected = original.copy()
    expected.pop(SMART_ROUTER_CONFIG_VERSION_ENV_VAR, None)
    if SMART_ROUTER_CONFIG_VERSION is not None:
        expected.update(_EXPECTED_PRESETS[SMART_ROUTER_CONFIG_VERSION])

    resolved = config.resolve_environment(source)

    assert resolved == expected
    assert source == original

    applied = source.copy()
    previous = config.apply_config(applied)

    assert applied == expected
    v2.restore_smart_routing_env(previous, applied)
    assert applied == original


@pytest.mark.parametrize(
    ("version", "expected_routing"),
    [
        (config.SUBAGENT_ONLY_V1, (True, False, False)),
        (config.SUBAGENT_ORCH_V1, (True, False, True)),
        (config.FIRST_PROMPT_AND_SUBAGENT_NO_ORCH_V0, (True, True, False)),
    ],
)
def test_legacy_preset_equivalence(version, expected_routing):
    legacy_env = _EXPECTED_PRESETS[version].copy()
    legacy_env["SMART_ROUTER_NAME"] = "m2-r315-quality-20260929"
    preset_env = {
        SMART_ROUTER_CONFIG_VERSION_ENV_VAR: version,
        "SMART_ROUTER_NAME": legacy_env["SMART_ROUTER_NAME"],
    }
    resolved = config.resolve_environment(preset_env)
    applied = preset_env.copy()
    config.apply_config(applied)
    assert applied == resolved
    for environment in (legacy_env, preset_env, resolved, applied):
        assert environment["SMART_ROUTER_NAME"] == legacy_env["SMART_ROUTER_NAME"]
        assert (
            v2.smart_routing_enabled(environment),
            v2.first_prompt_routing_enabled(environment),
            orchestrator.feature_enabled(environment),
        ) == expected_routing
