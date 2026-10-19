"""Component coverage for supported versioned smart-routing configuration."""

from __future__ import annotations

import pytest

from ucode.constants import (
    AGENT_CLAUDE,
    AGENT_CODEX,
    ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR,
    ENABLE_SMART_ROUTING_ENV_VAR,
    ENABLE_SMART_ROUTING_SAVINGS_ENV_VAR,
    ENABLE_SUBAGENT_ROUTING_ENV_VAR,
    SMART_ROUTER_CONFIG_VERSION_ENV_VAR,
)
from ucode.smart_routing import config, orchestrator, session_env, v2

_EXPECTED_PRESETS = {
    config.FIRST_PROMPT_AND_SUBAGENT_NO_ORCH_V0: {
        ENABLE_SMART_ROUTING_ENV_VAR: "1",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "0",
        ENABLE_SMART_ROUTING_SAVINGS_ENV_VAR: "0",
    },
    config.SUBAGENT_ONLY_V0: {
        ENABLE_SMART_ROUTING_ENV_VAR: "0",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "0",
        ENABLE_SMART_ROUTING_SAVINGS_ENV_VAR: "0",
    },
    config.SUBAGENT_ONLY_V1: {
        ENABLE_SMART_ROUTING_ENV_VAR: "1",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "0",
        ENABLE_SMART_ROUTING_SAVINGS_ENV_VAR: "0",
    },
    config.SUBAGENT_ORCH_V0: {
        ENABLE_SMART_ROUTING_ENV_VAR: "0",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "1",
        ENABLE_SMART_ROUTING_SAVINGS_ENV_VAR: "0",
    },
    config.SUBAGENT_ORCH_V1: {
        ENABLE_SMART_ROUTING_ENV_VAR: "1",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "1",
        ENABLE_SMART_ROUTING_SAVINGS_ENV_VAR: "0",
    },
    config.SUBAGENT_ORCH_SAVINGS_V0: {
        ENABLE_SMART_ROUTING_ENV_VAR: "1",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "1",
        ENABLE_SMART_ROUTING_SAVINGS_ENV_VAR: "1",
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
        ENABLE_SMART_ROUTING_SAVINGS_ENV_VAR: "0",
        "INHERITED_SETTING": "preserved",
    }
    expected = applied.copy()
    if selector is not None:
        applied[SMART_ROUTER_CONFIG_VERSION_ENV_VAR] = selector
    original = applied.copy()

    assert config.resolve_environment(applied, agent=AGENT_CLAUDE) == expected
    assert applied == original
    assert config.apply_config(applied, agent=AGENT_CLAUDE) == {}
    assert applied == original


@pytest.mark.parametrize(
    "legacy_values",
    [
        (None, None, None, None),
        ("0", "0", "0", "0"),
        ("1", "1", "1", "1"),
        ("1", "0", "1", "0"),
        ("0", "1", "0", "1"),
    ],
    ids=["unset", "disabled", "enabled", "mixed", "mixed-inverse"],
)
@pytest.mark.parametrize(
    "SMART_ROUTER_CONFIG_VERSION",
    [
        None,
        config.FIRST_PROMPT_AND_SUBAGENT_NO_ORCH_V0,
        config.SUBAGENT_ONLY_V0,
        config.SUBAGENT_ONLY_V1,
        config.SUBAGENT_ORCH_V0,
        config.SUBAGENT_ORCH_V1,
        config.SUBAGENT_ORCH_SAVINGS_V0,
    ],
)
def test_smart_routing_config_precedence(legacy_values, SMART_ROUTER_CONFIG_VERSION):
    source = {
        environment_key: value
        for environment_key, value in zip(
            (
                ENABLE_SMART_ROUTING_ENV_VAR,
                ENABLE_SUBAGENT_ROUTING_ENV_VAR,
                ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR,
                ENABLE_SMART_ROUTING_SAVINGS_ENV_VAR,
            ),
            legacy_values,
            strict=True,
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

    resolved = config.resolve_environment(source, agent=AGENT_CLAUDE)

    assert resolved == expected
    assert source == original

    applied = source.copy()
    previous = config.apply_config(applied, agent=AGENT_CLAUDE)

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
    resolved = config.resolve_environment(preset_env, agent=AGENT_CLAUDE)
    applied = preset_env.copy()
    config.apply_config(applied, agent=AGENT_CLAUDE)
    assert applied == resolved
    for environment in (legacy_env, preset_env, resolved, applied):
        assert environment["SMART_ROUTER_NAME"] == legacy_env["SMART_ROUTER_NAME"]
        assert (
            v2.smart_routing_enabled(environment, default=False, agent=AGENT_CLAUDE),
            v2.first_prompt_routing_enabled(environment, agent=AGENT_CLAUDE),
            orchestrator.feature_enabled(environment, agent=AGENT_CLAUDE),
        ) == expected_routing


@pytest.mark.parametrize(
    "failure", ["missing_agent", "unknown_agent", "missing_flag", "unknown_flag", "invalid_value"]
)
def test_agent_presets_require_complete_valid_configuration(failure):
    agents = {
        agent: flags.copy() for agent, flags in config._VERSIONS[config.SUBAGENT_ORCH_V0].items()
    }
    if failure == "missing_agent":
        agents.pop(AGENT_CODEX)
    elif failure == "unknown_agent":
        agents["other"] = agents[AGENT_CODEX].copy()
    elif failure == "missing_flag":
        agents[AGENT_CODEX].pop(ENABLE_SMART_ROUTING_ENV_VAR)
    elif failure == "unknown_flag":
        agents[AGENT_CODEX]["UNKNOWN_FLAG"] = "0"
    else:
        agents[AGENT_CODEX][ENABLE_SMART_ROUTING_ENV_VAR] = "yes"
    with pytest.raises(ValueError, match="Invalid smart-routing version"):
        config._validate_versions({"invalid_v0": agents})


def test_codex_uses_existing_presets():
    for version, expected in _EXPECTED_PRESETS.items():
        source = {SMART_ROUTER_CONFIG_VERSION_ENV_VAR: version}
        assert config.resolve_environment(source, agent=AGENT_CODEX) == expected


@pytest.mark.parametrize("agent, enabled", [(AGENT_CLAUDE, "1"), (AGENT_CODEX, "0")])
def test_agent_config_selection_and_session_precedence(monkeypatch, agent, enabled, tmp_path):
    monkeypatch.setitem(
        config._VERSIONS[config.SUBAGENT_ORCH_V0],
        AGENT_CODEX,
        {
            "ENABLE_SMART_ROUTING_V2": "0",
            "ENABLE_SMART_ROUTING_SUBAGENT_ONLY": "0",
            "ENABLE_SMART_ROUTER_ORCHESTRATOR": "0",
            "ENABLE_SMART_ROUTING_SAVINGS": "0",
        },
    )
    source = {"SMART_ROUTER_CONFIG_VERSION": "subagent_orch_v0"}
    expected = {
        "ENABLE_SMART_ROUTING_V2": "0",
        "ENABLE_SMART_ROUTING_SUBAGENT_ONLY": enabled,
        "ENABLE_SMART_ROUTER_ORCHESTRATOR": enabled,
        "ENABLE_SMART_ROUTING_SAVINGS": "0",
    }
    assert config.resolve_environment(source, agent=agent) == expected
    assert v2.smart_routing_enabled(source, default=True, agent=agent) == (enabled == "1")
    assert orchestrator.feature_enabled(source, agent=agent) == (enabled == "1")

    control = tmp_path / "session.json"
    control.write_text('{"ENABLE_SMART_ROUTER_ORCHESTRATOR":"0"}')
    source[session_env.SESSION_ENV_VAR] = str(control)
    assert session_env.effective_environment(source, agent=agent) == {
        **expected,
        session_env.SESSION_ENV_VAR: str(control),
        "ENABLE_SMART_ROUTER_ORCHESTRATOR": "0",
    }


@pytest.mark.parametrize("agent", [AGENT_CLAUDE, AGENT_CODEX])
def test_savings_preset_launch_and_session_off_restore_the_baseline(agent, tmp_path):
    source = {
        SMART_ROUTER_CONFIG_VERSION_ENV_VAR: config.SUBAGENT_ORCH_SAVINGS_V0,
        ENABLE_SMART_ROUTING_SAVINGS_ENV_VAR: "0",
    }
    original = source.copy()
    previous = v2.override_smart_routing(False, source, agent=agent)
    assert all(source[key] == "0" for key in config.SMART_ROUTING_ENV_KEYS)
    v2.restore_smart_routing_env(previous, source)
    assert source == original

    controls = tmp_path / "session.json"
    controls.write_text('{"ENABLE_SMART_ROUTING_SAVINGS":"0"}')
    source[session_env.SESSION_ENV_VAR] = str(controls)
    effective = session_env.effective_environment(source, agent=agent)
    assert effective[ENABLE_SMART_ROUTING_SAVINGS_ENV_VAR] == "0"
    assert effective[ENABLE_SUBAGENT_ROUTING_ENV_VAR] == "1"


def test_savings_alone_does_not_enable_routing():
    source = {ENABLE_SMART_ROUTING_SAVINGS_ENV_VAR: "1"}
    assert not v2.smart_routing_enabled(source, default=False, agent=AGENT_CLAUDE)
    assert not v2.first_prompt_routing_enabled(source, agent=AGENT_CLAUDE)
