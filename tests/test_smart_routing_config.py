"""Component coverage for supported versioned smart-routing configuration."""

from __future__ import annotations

import pytest

from ucode.constants import (
    AGENT_CLAUDE,
    AGENT_CODEX,
    ENABLE_SMART_ROUTER_AUTO_MODE_ENV_VAR,
    ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR,
    ENABLE_SMART_ROUTING_ENV_VAR,
    ENABLE_SUBAGENT_MODEL_IN_MAIN_TUI_ENV_VAR,
    ENABLE_SUBAGENT_ROUTING_ENV_VAR,
    SMART_ROUTER_CONFIG_VERSION_ENV_VAR,
)
from ucode.smart_routing import config, orchestrator, session_env, v2

_EXPECTED_PRESETS = {
    config.FIRST_PROMPT_AND_SUBAGENT_NO_ORCH_V0: {
        ENABLE_SMART_ROUTING_ENV_VAR: "1",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "0",
        ENABLE_SMART_ROUTER_AUTO_MODE_ENV_VAR: "0",
        ENABLE_SUBAGENT_MODEL_IN_MAIN_TUI_ENV_VAR: "1",
    },
    config.FIRST_PROMPT_AND_SUBAGENT_ORCH_V0: {
        ENABLE_SMART_ROUTING_ENV_VAR: "1",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "1",
        ENABLE_SMART_ROUTER_AUTO_MODE_ENV_VAR: "0",
        ENABLE_SUBAGENT_MODEL_IN_MAIN_TUI_ENV_VAR: "1",
    },
    config.SUBAGENT_ONLY_V0: {
        ENABLE_SMART_ROUTING_ENV_VAR: "0",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "0",
        ENABLE_SMART_ROUTER_AUTO_MODE_ENV_VAR: "0",
        ENABLE_SUBAGENT_MODEL_IN_MAIN_TUI_ENV_VAR: "1",
    },
    config.SUBAGENT_ONLY_V1: {
        ENABLE_SMART_ROUTING_ENV_VAR: "1",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "0",
        ENABLE_SMART_ROUTER_AUTO_MODE_ENV_VAR: "0",
        ENABLE_SUBAGENT_MODEL_IN_MAIN_TUI_ENV_VAR: "1",
    },
    config.SUBAGENT_ORCH_V0: {
        ENABLE_SMART_ROUTING_ENV_VAR: "0",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "1",
        ENABLE_SMART_ROUTER_AUTO_MODE_ENV_VAR: "0",
        ENABLE_SUBAGENT_MODEL_IN_MAIN_TUI_ENV_VAR: "1",
    },
    config.SUBAGENT_ORCH_V1: {
        ENABLE_SMART_ROUTING_ENV_VAR: "1",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "1",
        ENABLE_SMART_ROUTER_AUTO_MODE_ENV_VAR: "0",
        ENABLE_SUBAGENT_MODEL_IN_MAIN_TUI_ENV_VAR: "1",
    },
    config.SUBAGENT_ORCH_V0_BLIND_SUBAGENT_MODEL: {
        ENABLE_SMART_ROUTING_ENV_VAR: "0",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "1",
        ENABLE_SMART_ROUTER_AUTO_MODE_ENV_VAR: "0",
        ENABLE_SUBAGENT_MODEL_IN_MAIN_TUI_ENV_VAR: "0",
    },
}

# Versions whose mapping hides subagent model details in the main TUI, per agent.
_HIDDEN_MODEL_DETAIL_AGENTS = {
    config.SUBAGENT_AUTO_V0_CLAUDE_ONLY: {AGENT_CLAUDE},
    config.SUBAGENT_AUTO_V0: {AGENT_CLAUDE, AGENT_CODEX},
    config.SUBAGENT_ORCH_V0_BLIND_SUBAGENT_MODEL: {AGENT_CLAUDE, AGENT_CODEX},
    config.SUBAGENT_ORCH_V0_BLIND_SUBAGENT_MODEL_CLAUDE_ONLY: {AGENT_CLAUDE},
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

    assert config.resolve_environment(applied, agent=AGENT_CLAUDE) == expected
    assert applied == original
    assert config.apply_config(applied, agent=AGENT_CLAUDE) == {}
    assert applied == original


@pytest.mark.parametrize("ENABLE_SMART_ROUTING_V2", [None, "0", "1"])
@pytest.mark.parametrize("ENABLE_SMART_ROUTING_SUBAGENT_ONLY", [None, "0", "1"])
@pytest.mark.parametrize("ENABLE_SMART_ROUTER_ORCHESTRATOR", [None, "0", "1"])
@pytest.mark.parametrize(
    "SMART_ROUTER_CONFIG_VERSION",
    [
        None,
        config.FIRST_PROMPT_AND_SUBAGENT_NO_ORCH_V0,
        config.FIRST_PROMPT_AND_SUBAGENT_ORCH_V0,
        config.SUBAGENT_ONLY_V0,
        config.SUBAGENT_ONLY_V1,
        config.SUBAGENT_ORCH_V0,
        config.SUBAGENT_ORCH_V1,
        config.SUBAGENT_ORCH_V0_BLIND_SUBAGENT_MODEL,
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
        (config.FIRST_PROMPT_AND_SUBAGENT_ORCH_V0, (True, True, True)),
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


@pytest.mark.parametrize(
    "version",
    [
        config.SUBAGENT_ORCH_V0_CLAUDE_ONLY,
        config.SUBAGENT_ORCH_V0_BLIND_SUBAGENT_MODEL_CLAUDE_ONLY,
        config.SUBAGENT_AUTO_V0_CLAUDE_ONLY,
        config.FIRST_PROMPT_AND_SUBAGENT_ORCH_V0_CLAUDE_ONLY,
    ],
)
def test_claude_only_versions_turn_codex_routing_off(version):
    source = {SMART_ROUTER_CONFIG_VERSION_ENV_VAR: version}

    assert config.resolve_environment(source, agent=AGENT_CODEX) == {
        **dict.fromkeys(
            (
                ENABLE_SMART_ROUTING_ENV_VAR,
                ENABLE_SUBAGENT_ROUTING_ENV_VAR,
                ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR,
                ENABLE_SMART_ROUTER_AUTO_MODE_ENV_VAR,
            ),
            "0",
        ),
        ENABLE_SUBAGENT_MODEL_IN_MAIN_TUI_ENV_VAR: "1",
    }


@pytest.mark.parametrize(
    ("version", "agent"),
    [
        (config.FIRST_PROMPT_AND_SUBAGENT_ORCH_V0, AGENT_CLAUDE),
        (config.FIRST_PROMPT_AND_SUBAGENT_ORCH_V0, AGENT_CODEX),
        (config.FIRST_PROMPT_AND_SUBAGENT_ORCH_V0_CLAUDE_ONLY, AGENT_CLAUDE),
    ],
)
def test_first_prompt_orch_routes_the_first_prompt_with_orchestration(version, agent):
    source = {SMART_ROUTER_CONFIG_VERSION_ENV_VAR: version}

    assert (
        config.resolve_environment(source, agent=agent)
        == _EXPECTED_PRESETS[config.FIRST_PROMPT_AND_SUBAGENT_ORCH_V0]
    )
    assert v2.first_prompt_routing_enabled(source, agent=agent)
    assert orchestrator.feature_enabled(source, agent=agent)
    assert not config.auto_mode_enabled(source)


def test_blind_orch_claude_only_claude_matches_blind_preset():
    source = {
        SMART_ROUTER_CONFIG_VERSION_ENV_VAR: config.SUBAGENT_ORCH_V0_BLIND_SUBAGENT_MODEL_CLAUDE_ONLY
    }
    assert (
        config.resolve_environment(source, agent=AGENT_CLAUDE)
        == _EXPECTED_PRESETS[config.SUBAGENT_ORCH_V0_BLIND_SUBAGENT_MODEL]
    )


@pytest.mark.parametrize("hidden", ["0", "1", None])
def test_subagent_model_visible_only_when_not_zero(hidden):
    env = {} if hidden is None else {ENABLE_SUBAGENT_MODEL_IN_MAIN_TUI_ENV_VAR: hidden}
    assert config.subagent_model_visible(env) is (hidden != "0")


@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        (None, False),
        ("", False),
        ("   ", False),
        ("unsupported_version", False),
        (f" {config.SUBAGENT_ONLY_V0} ", True),
        *((version, True) for version in sorted(config._VERSIONS)),
    ],
)
def test_known_version(selector, expected):
    env = {} if selector is None else {SMART_ROUTER_CONFIG_VERSION_ENV_VAR: selector}
    assert config.known_version(env) is expected


@pytest.mark.parametrize("agent", [AGENT_CLAUDE, AGENT_CODEX])
@pytest.mark.parametrize("selector", [None, "", "   ", "unsupported_version"])
def test_enabled_version_selects_shared_preset_over_legacy_flags(agent, selector):
    inherited = {
        ENABLE_SMART_ROUTING_ENV_VAR: "1",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "0",
        "INHERITED_SETTING": "preserved",
    }
    if selector is not None:
        inherited[SMART_ROUTER_CONFIG_VERSION_ENV_VAR] = selector
    applied = inherited.copy()

    previous = config.apply_enabled_version(applied, agent=agent)

    expected = _EXPECTED_PRESETS[config.SUBAGENT_ORCH_V0]
    assert applied == {
        **expected,
        "INHERITED_SETTING": "preserved",
    }
    v2.restore_smart_routing_env(previous, applied)
    assert applied == inherited


@pytest.mark.parametrize("agent", [AGENT_CLAUDE, AGENT_CODEX])
@pytest.mark.parametrize("version", sorted(config._VERSIONS))
def test_enabled_version_keeps_a_known_version(agent, version):
    inherited = {
        ENABLE_SMART_ROUTING_ENV_VAR: "1",
        SMART_ROUTER_CONFIG_VERSION_ENV_VAR: version,
        ENABLE_SMART_ROUTER_AUTO_MODE_ENV_VAR: "1",
    }
    applied = inherited.copy()

    previous = config.apply_enabled_version(applied, agent=agent)

    assert applied == config.resolve_environment(inherited, agent=agent)
    assert applied[ENABLE_SMART_ROUTER_AUTO_MODE_ENV_VAR] == (
        "1"
        if (version, agent)
        in {
            (config.SUBAGENT_AUTO_V0_CLAUDE_ONLY, AGENT_CLAUDE),
            (config.SUBAGENT_AUTO_V0, AGENT_CLAUDE),
            (config.SUBAGENT_AUTO_V0, AGENT_CODEX),
        }
        else "0"
    )
    v2.restore_smart_routing_env(previous, applied)
    assert applied == inherited


@pytest.mark.parametrize(
    ("selector", "routes"),
    [
        (None, {AGENT_CLAUDE: True, AGENT_CODEX: True}),
        ("unsupported_version", {AGENT_CLAUDE: True, AGENT_CODEX: True}),
        (config.SUBAGENT_ORCH_V0_CLAUDE_ONLY, {AGENT_CLAUDE: True, AGENT_CODEX: False}),
        (
            config.SUBAGENT_ORCH_V0_BLIND_SUBAGENT_MODEL_CLAUDE_ONLY,
            {AGENT_CLAUDE: True, AGENT_CODEX: False},
        ),
        (config.SUBAGENT_AUTO_V0_CLAUDE_ONLY, {AGENT_CLAUDE: True, AGENT_CODEX: False}),
        (config.SUBAGENT_AUTO_V0, {AGENT_CLAUDE: True, AGENT_CODEX: True}),
        (
            config.FIRST_PROMPT_AND_SUBAGENT_ORCH_V0_CLAUDE_ONLY,
            {AGENT_CLAUDE: True, AGENT_CODEX: False},
        ),
        *(
            (version, {AGENT_CLAUDE: True, AGENT_CODEX: True})
            for version in sorted(
                set(config._VERSIONS)
                - {
                    config.SUBAGENT_ORCH_V0_CLAUDE_ONLY,
                    config.SUBAGENT_ORCH_V0_BLIND_SUBAGENT_MODEL_CLAUDE_ONLY,
                    config.SUBAGENT_AUTO_V0_CLAUDE_ONLY,
                    config.SUBAGENT_AUTO_V0,
                    config.FIRST_PROMPT_AND_SUBAGENT_ORCH_V0_CLAUDE_ONLY,
                }
            )
        ),
    ],
)
def test_enabled_version_routes(selector, routes):
    env = {ENABLE_SMART_ROUTING_ENV_VAR: "1", ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1"}
    if selector is not None:
        env[SMART_ROUTER_CONFIG_VERSION_ENV_VAR] = selector
    before = env.copy()

    for agent, expected in routes.items():
        assert config.enabled_version_routes(env, agent=agent) is expected
        applied = env.copy()
        config.apply_enabled_version(applied, agent=agent)
        assert v2.smart_routing_enabled(applied, default=False, agent=agent) is expected
    assert env == before


@pytest.mark.parametrize("agent, enabled", [(AGENT_CLAUDE, "1"), (AGENT_CODEX, "0")])
def test_agent_config_selection_and_session_precedence(monkeypatch, agent, enabled, tmp_path):
    monkeypatch.setitem(
        config._VERSIONS[config.SUBAGENT_ORCH_V0],
        AGENT_CODEX,
        {
            "ENABLE_SMART_ROUTING_V2": "0",
            "ENABLE_SMART_ROUTING_SUBAGENT_ONLY": "0",
            "ENABLE_SMART_ROUTER_ORCHESTRATOR": "0",
            "ENABLE_SMART_ROUTER_AUTO_MODE": "0",
            "ENABLE_SUBAGENT_MODEL_IN_MAIN_TUI": "1",
        },
    )
    source = {"SMART_ROUTER_CONFIG_VERSION": "subagent_orch_v0"}
    expected = {
        "ENABLE_SMART_ROUTING_V2": "0",
        "ENABLE_SMART_ROUTING_SUBAGENT_ONLY": enabled,
        "ENABLE_SMART_ROUTER_ORCHESTRATOR": enabled,
        "ENABLE_SMART_ROUTER_AUTO_MODE": "0",
        "ENABLE_SUBAGENT_MODEL_IN_MAIN_TUI": "1",
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
def test_unreadable_controls_fallback_is_silent(tmp_path, capsys, agent):
    controls = tmp_path / "controls.json"
    controls.write_text("{invalid")
    source = {session_env.SESSION_ENV_VAR: str(controls), "ENABLE_SMART_ROUTING_SUBAGENT_ONLY": "1"}
    assert session_env.effective_environment(source, agent=agent) == source
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("shell_value", ["0", "1"])
@pytest.mark.parametrize(
    ("version", "harnesses"),
    [
        (config.FIRST_PROMPT_AND_SUBAGENT_NO_ORCH_V0, (AGENT_CLAUDE, AGENT_CODEX)),
        (config.FIRST_PROMPT_AND_SUBAGENT_ORCH_V0, (AGENT_CLAUDE, AGENT_CODEX)),
        (config.SUBAGENT_ONLY_V0, (AGENT_CLAUDE, AGENT_CODEX)),
        (config.SUBAGENT_ONLY_V1, (AGENT_CLAUDE, AGENT_CODEX)),
        (config.SUBAGENT_ORCH_V0, (AGENT_CLAUDE, AGENT_CODEX)),
        (config.SUBAGENT_ORCH_V1, (AGENT_CLAUDE, AGENT_CODEX)),
        (config.SUBAGENT_ORCH_V0_BLIND_SUBAGENT_MODEL, (AGENT_CLAUDE, AGENT_CODEX)),
        (config.SUBAGENT_ORCH_V0_CLAUDE_ONLY, (AGENT_CLAUDE,)),
        (config.SUBAGENT_ORCH_V0_BLIND_SUBAGENT_MODEL_CLAUDE_ONLY, (AGENT_CLAUDE,)),
        (config.FIRST_PROMPT_AND_SUBAGENT_ORCH_V0_CLAUDE_ONLY, (AGENT_CLAUDE,)),
    ],
)
def test_describe_routing_reports_each_version_over_shell_flags(version, harnesses, shell_value):
    source = {
        SMART_ROUTER_CONFIG_VERSION_ENV_VAR: f" {version} ",
        **dict.fromkeys(
            (
                ENABLE_SMART_ROUTING_ENV_VAR,
                ENABLE_SUBAGENT_ROUTING_ENV_VAR,
                ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR,
            ),
            shell_value,
        ),
    }
    original = source.copy()

    assert v2.describe_routing(source) == v2.RoutingDescription(harnesses=harnesses)
    assert source == original


@pytest.mark.parametrize(
    ("source", "harnesses"),
    [
        ({}, ()),
        ({SMART_ROUTER_CONFIG_VERSION_ENV_VAR: "  "}, ()),
        ({ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "1"}, ()),
        (
            {
                SMART_ROUTER_CONFIG_VERSION_ENV_VAR: "unsupported_version",
                ENABLE_SMART_ROUTING_ENV_VAR: "1",
            },
            (AGENT_CLAUDE, AGENT_CODEX),
        ),
        ({ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1"}, (AGENT_CLAUDE, AGENT_CODEX)),
    ],
)
def test_describe_routing_without_a_known_version_reports_shell_flags(source, harnesses):
    assert v2.describe_routing(source) == v2.RoutingDescription(harnesses=harnesses)


def test_enabled_version_routes_ignores_uncovered_agent():
    env = {SMART_ROUTER_CONFIG_VERSION_ENV_VAR: config.SUBAGENT_ORCH_V0_CLAUDE_ONLY}

    assert config.enabled_version_routes(env, agent="opencode") is False
    assert config.enabled_version_routes({}, agent="opencode") is False


def test_auto_flag_alone_does_not_enable_routing():
    assert not v2.smart_routing_enabled(
        {ENABLE_SMART_ROUTER_AUTO_MODE_ENV_VAR: "1"}, default=False, agent=AGENT_CLAUDE
    )


@pytest.mark.parametrize("agent", [AGENT_CLAUDE, AGENT_CODEX])
@pytest.mark.parametrize("version", [None, "unknown_v99", *config._VERSIONS])
@pytest.mark.parametrize("inherited", [None, "0", "1"])
def test_subagent_model_display_version_precedence_and_restoration(agent, version, inherited):
    source = {}
    if version is not None:
        source[SMART_ROUTER_CONFIG_VERSION_ENV_VAR] = version
    if inherited is not None:
        source[ENABLE_SUBAGENT_MODEL_IN_MAIN_TUI_ENV_VAR] = inherited
    original = source.copy()
    expected = inherited
    if version in config._VERSIONS:
        expected = "0" if agent in _HIDDEN_MODEL_DETAIL_AGENTS.get(version, set()) else "1"
    assert (
        config.resolve_environment(source, agent=agent).get(
            ENABLE_SUBAGENT_MODEL_IN_MAIN_TUI_ENV_VAR
        )
        == expected
    )
    previous = config.apply_config(source, agent=agent)
    assert source.get(ENABLE_SUBAGENT_MODEL_IN_MAIN_TUI_ENV_VAR) == expected
    v2.restore_smart_routing_env(previous, source)
    assert source == original


def test_subagent_model_display_alone_does_not_enable_routing():
    assert not v2.smart_routing_enabled(
        {ENABLE_SUBAGENT_MODEL_IN_MAIN_TUI_ENV_VAR: "1"}, default=False, agent=AGENT_CLAUDE
    )


@pytest.mark.parametrize("version", [None, "", "unknown_v99", *config._VERSIONS])
@pytest.mark.parametrize("legacy_flag", ["0", "1"])
def test_auto_requires_exact_version(version, legacy_flag):
    env = {ENABLE_SMART_ROUTER_AUTO_MODE_ENV_VAR: legacy_flag}
    if version is not None:
        env[SMART_ROUTER_CONFIG_VERSION_ENV_VAR] = version
    assert config.auto_mode_enabled(env) is (
        version in (config.SUBAGENT_AUTO_V0, config.SUBAGENT_AUTO_V0_CLAUDE_ONLY)
    )


def test_version_scope_retains_identity_without_reapplying_flags(monkeypatch):
    monkeypatch.setenv(SMART_ROUTER_CONFIG_VERSION_ENV_VAR, config.SUBAGENT_AUTO_V0_CLAUDE_ONLY)
    with config.version_scope():
        previous = config.apply_config(agent=AGENT_CLAUDE)
        try:
            monkeypatch.setenv(ENABLE_SUBAGENT_ROUTING_ENV_VAR, "0")
            with config.version_scope():
                assert config.auto_mode_enabled()
                assert not config.auto_mode_enabled({})
                assert (
                    config.resolve_environment(agent=AGENT_CLAUDE)[ENABLE_SUBAGENT_ROUTING_ENV_VAR]
                    == "0"
                )
        finally:
            v2.restore_smart_routing_env(previous)
    monkeypatch.delenv(SMART_ROUTER_CONFIG_VERSION_ENV_VAR)
    assert not config.auto_mode_enabled()


@pytest.mark.parametrize("override", ["", "unknown_v99", config.SUBAGENT_ONLY_V0])
def test_nested_version_scope_overrides_and_restores_identity(monkeypatch, override):
    monkeypatch.setenv(SMART_ROUTER_CONFIG_VERSION_ENV_VAR, config.SUBAGENT_AUTO_V0_CLAUDE_ONLY)
    with config.version_scope():
        monkeypatch.delenv(SMART_ROUTER_CONFIG_VERSION_ENV_VAR)
        with pytest.raises(RuntimeError, match="stop"):
            with monkeypatch.context() as inner:
                inner.setenv(SMART_ROUTER_CONFIG_VERSION_ENV_VAR, override)
                with config.version_scope():
                    assert not config.auto_mode_enabled()
                    raise RuntimeError("stop")
        assert config.auto_mode_enabled()
    assert not config.auto_mode_enabled()
