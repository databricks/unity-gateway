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


@pytest.mark.parametrize("selector", ["unsupported_version", " unsupported_version "])
def test_unknown_selector_is_a_no_op(selector):
    applied = {
        SMART_ROUTER_CONFIG_VERSION_ENV_VAR: selector,
        ENABLE_SMART_ROUTING_ENV_VAR: "1",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "1",
        "INHERITED_SETTING": "preserved",
    }
    original = applied.copy()

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
    config.apply_config(applied)

    assert applied == expected


@pytest.mark.parametrize(
    ("legacy_env", "version", "expected_routing"),
    [
        pytest.param(
            {
                ENABLE_SMART_ROUTING_ENV_VAR: "1",
                ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
                "SMART_ROUTER_NAME": "m2-r315-quality-20260929",
            },
            config.SUBAGENT_ONLY_V1,
            (True, False, False),
            id="subagent-only-v1",
        ),
        pytest.param(
            {
                ENABLE_SMART_ROUTING_ENV_VAR: "1",
                ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
                ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "1",
                "SMART_ROUTER_NAME": "m2-r315-quality-20260929",
            },
            config.SUBAGENT_ORCH_V1,
            (True, False, True),
            id="subagent-orch-v1",
        ),
        pytest.param(
            {ENABLE_SMART_ROUTING_ENV_VAR: "1"},
            config.FIRST_PROMPT_AND_SUBAGENT_NO_ORCH_V0,
            (True, True, False),
            id="customer-first-prompt-and-subagent",
        ),
    ],
)
def test_legacy_preset_equivalence(legacy_env, version, expected_routing):
    preset_env = {SMART_ROUTER_CONFIG_VERSION_ENV_VAR: version}
    if "SMART_ROUTER_NAME" in legacy_env:
        preset_env["SMART_ROUTER_NAME"] = legacy_env["SMART_ROUTER_NAME"]
    original_legacy = legacy_env.copy()
    original_preset = preset_env.copy()
    expected_flags = _EXPECTED_PRESETS[version]

    assert {key: legacy_env.get(key, "0") for key in expected_flags} == expected_flags

    resolved = config.resolve_environment(preset_env)
    applied = preset_env.copy()
    config.apply_config(applied)

    for environment in (resolved, applied):
        assert {key: environment[key] for key in expected_flags} == expected_flags
        assert environment.get("SMART_ROUTER_NAME") == legacy_env.get("SMART_ROUTER_NAME")
        assert SMART_ROUTER_CONFIG_VERSION_ENV_VAR not in environment
    assert applied == resolved

    for environment in (legacy_env, preset_env, resolved, applied):
        assert (
            v2.smart_routing_enabled(environment),
            v2.first_prompt_routing_enabled(environment),
            orchestrator.feature_enabled(environment),
        ) == expected_routing

    assert legacy_env == original_legacy
    assert preset_env == original_preset


@pytest.mark.parametrize("selector", [None, "", "   "], ids=["missing", "empty", "whitespace"])
def test_missing_selector_is_a_no_op(selector):
    applied = {
        ENABLE_SMART_ROUTING_ENV_VAR: "1",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "1",
        "INHERITED_SETTING": "preserved",
    }
    if selector is not None:
        applied[SMART_ROUTER_CONFIG_VERSION_ENV_VAR] = selector
    original = applied.copy()

    assert config.apply_config(applied) == {}
    assert applied == original


@pytest.mark.parametrize("selector", ["unsupported_version", " unsupported_version "])
def test_resolve_unknown_selector_omits_selector_without_mutating_input(selector):
    source = {
        SMART_ROUTER_CONFIG_VERSION_ENV_VAR: selector,
        ENABLE_SMART_ROUTING_ENV_VAR: "1",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "1",
        "INHERITED_SETTING": "preserved",
    }

    assert config.resolve_environment(source) == {
        ENABLE_SMART_ROUTING_ENV_VAR: "1",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "1",
        "INHERITED_SETTING": "preserved",
    }
    assert source == {
        SMART_ROUTER_CONFIG_VERSION_ENV_VAR: selector,
        ENABLE_SMART_ROUTING_ENV_VAR: "1",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "1",
        "INHERITED_SETTING": "preserved",
    }


@pytest.mark.parametrize(("version", "preset"), _EXPECTED_PRESETS.items())
def test_resolve_known_selector_applies_preset_without_mutating_input(version, preset):
    source = {
        SMART_ROUTER_CONFIG_VERSION_ENV_VAR: version,
        ENABLE_SMART_ROUTING_ENV_VAR: "0",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "0",
        "INHERITED_SETTING": "preserved",
    }
    original = source.copy()
    expected = source.copy()
    expected.pop(SMART_ROUTER_CONFIG_VERSION_ENV_VAR)
    expected.update(preset)

    assert config.resolve_environment(source) == expected
    assert source == original


@pytest.mark.parametrize("version", _EXPECTED_PRESETS)
def test_apply_config_valid_selector_applies_and_restores(version):
    original = {
        SMART_ROUTER_CONFIG_VERSION_ENV_VAR: version,
        ENABLE_SMART_ROUTING_ENV_VAR: "0",
        "INHERITED_SETTING": "preserved",
    }
    expected = original.copy()
    expected.pop(SMART_ROUTER_CONFIG_VERSION_ENV_VAR)
    expected.update(_EXPECTED_PRESETS[version])
    applied = original.copy()

    previous = config.apply_config(applied)

    assert applied == expected
    assert previous == {
        ENABLE_SMART_ROUTING_ENV_VAR: "0",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: None,
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: None,
        SMART_ROUTER_CONFIG_VERSION_ENV_VAR: version,
    }

    v2.restore_smart_routing_env(previous, applied)
    assert applied == original
