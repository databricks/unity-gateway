"""Component coverage for versioned smart-routing configuration."""

from __future__ import annotations

import os
import runpy
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

import ucode.cli as cli
import ucode.constants as constants
from ucode.constants import (
    ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR,
    ENABLE_SMART_ROUTING_ENV_VAR,
    ENABLE_SUBAGENT_ROUTING_ENV_VAR,
    SMART_ROUTING_CONFIG_ENV_KEYS,
    SMART_ROUTING_CONFIG_VERSION_ENV_VAR,
)
from ucode.smart_routing import config, orchestrator, session_env, v2

runner = CliRunner()


@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        (
            "subagent_only_v0",
            {
                ENABLE_SMART_ROUTING_ENV_VAR: "0",
                ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
                ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "0",
            },
        ),
        (
            "subagent_orch_v0",
            {
                ENABLE_SMART_ROUTING_ENV_VAR: "0",
                ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
                ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "1",
            },
        ),
    ],
)
def test_resolve_environment_materializes_selector_without_mutating_input(selector, expected):
    source = {
        SMART_ROUTING_CONFIG_VERSION_ENV_VAR: selector,
        ENABLE_SMART_ROUTING_ENV_VAR: "1",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "0",
        "UNRELATED_SETTING": "preserved",
    }

    resolved = config.resolve_environment(source)

    assert resolved == {**expected, "UNRELATED_SETTING": "preserved"}
    assert source == {
        SMART_ROUTING_CONFIG_VERSION_ENV_VAR: selector,
        ENABLE_SMART_ROUTING_ENV_VAR: "1",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "0",
        "UNRELATED_SETTING": "preserved",
    }


@pytest.mark.parametrize(
    ("selector", "expected_orchestrator"),
    [("subagent_only_v0", "0"), ("subagent_orch_v0", "1")],
)
def test_resolve_environment_supports_canonical_v0_selectors(selector, expected_orchestrator):
    resolved = config.resolve_environment({SMART_ROUTING_CONFIG_VERSION_ENV_VAR: selector})

    assert resolved == {
        ENABLE_SMART_ROUTING_ENV_VAR: "0",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: expected_orchestrator,
    }


@pytest.mark.parametrize("selector", ["subagent_only", "subagent_orch"])
@pytest.mark.parametrize("resolver", [config.resolve_environment, config.apply_config])
def test_unsuffixed_selectors_are_rejected_without_mutating_input(selector, resolver):
    environment = {
        SMART_ROUTING_CONFIG_VERSION_ENV_VAR: selector,
        "UNRELATED_SETTING": "preserved",
    }
    original = environment.copy()

    with pytest.raises(RuntimeError):
        resolver(environment)

    assert environment == original


@pytest.mark.parametrize("selector", [None, "", " \t"])
def test_resolve_environment_uses_legacy_flags_for_blank_or_unset_selector(selector):
    source = {
        ENABLE_SMART_ROUTING_ENV_VAR: "1",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "1",
    }
    if selector is not None:
        source[SMART_ROUTING_CONFIG_VERSION_ENV_VAR] = selector
    original = source.copy()

    resolved = config.resolve_environment(source)

    assert resolved == {
        ENABLE_SMART_ROUTING_ENV_VAR: "1",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "1",
    }
    assert source == original


def test_legacy_flags_still_drive_routing_queries_without_selector():
    environment = {
        ENABLE_SMART_ROUTING_ENV_VAR: "1",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "1",
    }

    assert config.resolve_environment(environment) == environment
    assert v2.smart_routing_enabled(environment) is True
    assert v2.first_prompt_routing_enabled(environment) is True
    assert orchestrator.feature_enabled(environment) is True


@pytest.mark.parametrize("selector", ["subagent_only_v0", "subagent_orch_v0"])
def test_selector_wins_legacy_conflicts_for_routing_queries(selector):
    environment = {
        SMART_ROUTING_CONFIG_VERSION_ENV_VAR: selector,
        ENABLE_SMART_ROUTING_ENV_VAR: "1",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "0",
    }

    assert v2.smart_routing_enabled(environment) is True
    assert v2.first_prompt_routing_enabled(environment) is False
    assert orchestrator.feature_enabled(environment) is (selector == "subagent_orch_v0")


def test_apply_config_materializes_selector_and_returns_previous_owned_values():
    environment = {
        SMART_ROUTING_CONFIG_VERSION_ENV_VAR: "subagent_only_v0",
        ENABLE_SMART_ROUTING_ENV_VAR: "old-v2",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "old-subagent",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "old-orchestrator",
        "UNRELATED_SETTING": "preserved",
    }

    previous = config.apply_config(environment)

    assert set(previous) == {
        SMART_ROUTING_CONFIG_VERSION_ENV_VAR,
        *SMART_ROUTING_CONFIG_ENV_KEYS,
    }
    assert previous == {
        SMART_ROUTING_CONFIG_VERSION_ENV_VAR: "subagent_only_v0",
        ENABLE_SMART_ROUTING_ENV_VAR: "old-v2",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "old-subagent",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "old-orchestrator",
    }
    assert environment == {
        ENABLE_SMART_ROUTING_ENV_VAR: "0",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "0",
        "UNRELATED_SETTING": "preserved",
    }


@pytest.mark.parametrize("selector", ["subagent_only_v1", "subagent_orch_v1"])
@pytest.mark.parametrize("resolver", [config.resolve_environment, config.apply_config])
def test_future_v1_selectors_remain_unknown(selector, resolver):
    with pytest.raises(RuntimeError):
        resolver({SMART_ROUTING_CONFIG_VERSION_ENV_VAR: selector})


@pytest.mark.parametrize("selector", [None, "", " "])
def test_apply_config_is_a_noop_for_unset_or_blank_selector(selector):
    environment = {"UNRELATED_SETTING": "preserved"}
    if selector is not None:
        environment[SMART_ROUTING_CONFIG_VERSION_ENV_VAR] = selector
    original = environment.copy()

    assert config.apply_config(environment) == {}
    assert environment == original


def test_unknown_selector_mentions_supported_names_and_does_not_mutate_input():
    for resolver in (config.resolve_environment, config.apply_config):
        environment = {SMART_ROUTING_CONFIG_VERSION_ENV_VAR: "future_mode"}

        with pytest.raises(RuntimeError) as caught:
            resolver(environment)

        message = str(caught.value)
        assert "future_mode" in message
        assert "subagent_only_v0" in message
        assert "subagent_orch_v0" in message
        assert environment == {SMART_ROUTING_CONFIG_VERSION_ENV_VAR: "future_mode"}


@pytest.mark.parametrize("missing_key", SMART_ROUTING_CONFIG_ENV_KEYS)
def test_validate_versions_rejects_each_missing_managed_flag(missing_key):
    values = {key: "0" for key in SMART_ROUTING_CONFIG_ENV_KEYS if key != missing_key}

    with pytest.raises(ValueError):
        config._validate_versions({"test_version": values})


@pytest.mark.parametrize("invalid_value", [None, "", "2", "true", 0, False])
def test_validate_versions_rejects_non_binary_managed_flag_values(invalid_value):
    values = dict.fromkeys(SMART_ROUTING_CONFIG_ENV_KEYS, "0")
    values[ENABLE_SMART_ROUTING_ENV_VAR] = invalid_value

    with pytest.raises(ValueError):
        config._validate_versions({"test_version": values})


def test_validate_versions_rejects_unexpected_managed_flag():
    values = dict.fromkeys(SMART_ROUTING_CONFIG_ENV_KEYS, "0")
    values["UNEXPECTED_SMART_ROUTING_FLAG"] = "0"

    with pytest.raises(ValueError):
        config._validate_versions({"test_version": values})


def test_config_import_rejects_new_registry_flag_before_runtime_use(monkeypatch):
    new_key = "ENABLE_SMART_ROUTING_TEST_ONLY"
    monkeypatch.setattr(
        constants,
        "SMART_ROUTING_CONFIG_ENV_KEYS",
        (*constants.SMART_ROUTING_CONFIG_ENV_KEYS, new_key),
    )

    with pytest.raises(ValueError):
        runpy.run_path(config.__file__)


@pytest.mark.parametrize("operation", ["enable", "override", "disable"])
def test_v2_routing_toggles_restore_selector_and_legacy_environment(operation):
    original = {
        SMART_ROUTING_CONFIG_VERSION_ENV_VAR: "subagent_orch_v0",
        "UNRELATED_SETTING": "preserved",
    }
    environment = original.copy()

    if operation == "enable":
        previous = v2.enable_smart_routing(environment)
        expected = {
            ENABLE_SMART_ROUTING_ENV_VAR: "1",
            ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
            ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "1",
            "UNRELATED_SETTING": "preserved",
        }
    elif operation == "override":
        previous = v2.override_smart_routing(True, environment)
        expected = {
            ENABLE_SMART_ROUTING_ENV_VAR: "1",
            ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
            ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "1",
            "UNRELATED_SETTING": "preserved",
        }
    else:
        previous = v2.disable_smart_routing(environment)
        expected = {"UNRELATED_SETTING": "preserved"}

    assert environment == expected
    assert SMART_ROUTING_CONFIG_VERSION_ENV_VAR in previous

    v2.restore_smart_routing_env(previous, environment)

    assert environment == original


def test_explicit_disable_wins_over_selector_until_restored():
    original = {SMART_ROUTING_CONFIG_VERSION_ENV_VAR: "subagent_orch_v0"}
    environment = original.copy()

    previous = v2.override_smart_routing(False, environment)

    assert SMART_ROUTING_CONFIG_VERSION_ENV_VAR not in environment
    assert environment[ENABLE_SMART_ROUTING_ENV_VAR] == "0"
    assert environment[ENABLE_SUBAGENT_ROUTING_ENV_VAR] == "0"
    assert v2.smart_routing_enabled(environment) is False
    assert v2.first_prompt_routing_enabled(environment) is False

    v2.restore_smart_routing_env(previous, environment)

    assert environment == original


def test_cli_context_materializes_inherited_selector_and_restores_after_failure(monkeypatch):
    original = {
        SMART_ROUTING_CONFIG_VERSION_ENV_VAR: "subagent_orch_v0",
        ENABLE_SMART_ROUTING_ENV_VAR: "old-v2",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "old-subagent",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "old-orchestrator",
    }
    for key, value in original.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(ValueError, match="launch failed"):
        with cli._smart_routing_v2_flag(None):
            assert SMART_ROUTING_CONFIG_VERSION_ENV_VAR not in os.environ
            assert os.environ[ENABLE_SMART_ROUTING_ENV_VAR] == "0"
            assert os.environ[ENABLE_SUBAGENT_ROUTING_ENV_VAR] == "1"
            assert os.environ[ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR] == "1"
            assert v2.smart_routing_enabled() is True
            raise ValueError("launch failed")

    assert {key: os.environ.get(key) for key in original} == original


def test_cli_context_turns_invalid_selector_into_actionable_exit(monkeypatch):
    monkeypatch.setenv(SMART_ROUTING_CONFIG_VERSION_ENV_VAR, "future_mode")

    with pytest.raises(typer.Exit) as caught:
        with cli._smart_routing_v2_flag(None):
            pytest.fail("invalid selector should prevent entering the context")

    assert caught.value.exit_code == 1
    assert os.environ[SMART_ROUTING_CONFIG_VERSION_ENV_VAR] == "future_mode"


def test_bare_launch_materializes_selector_for_managed_default_and_restores_environment(
    monkeypatch,
):
    original = {
        SMART_ROUTING_CONFIG_VERSION_ENV_VAR: "subagent_orch_v0",
        ENABLE_SMART_ROUTING_ENV_VAR: "1",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "0",
    }
    for key, value in original.items():
        monkeypatch.setenv(key, value)
    observed = []

    def inspect_managed_default(*_args, **_kwargs):
        observed.append(
            {
                "selector": os.environ.get(SMART_ROUTING_CONFIG_VERSION_ENV_VAR),
                "v2": os.environ.get(ENABLE_SMART_ROUTING_ENV_VAR),
                "subagent": os.environ.get(ENABLE_SUBAGENT_ROUTING_ENV_VAR),
                "orchestrator": os.environ.get(ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR),
            }
        )

    with patch.object(
        cli, "_launch_managed_default", side_effect=inspect_managed_default
    ) as launch:
        result = runner.invoke(cli.app, [])

    assert result.exit_code == 0, result.output
    launch.assert_called_once()
    assert observed == [
        {
            "selector": None,
            "v2": "0",
            "subagent": "1",
            "orchestrator": "1",
        }
    ]
    assert {key: os.environ.get(key) for key in original} == original


def test_bare_launch_rejects_invalid_selector_before_managed_default(monkeypatch):
    monkeypatch.setenv(SMART_ROUTING_CONFIG_VERSION_ENV_VAR, "future_mode")

    with patch.object(cli, "_launch_managed_default") as launch:
        result = runner.invoke(cli.app, [])

    assert result.exit_code == 1
    launch.assert_not_called()
    assert "subagent_only_v0" in result.output
    assert "subagent_orch_v0" in result.output


@pytest.mark.parametrize(
    ("tool", "subcommand"),
    [("codex", "app"), ("claude", "update")],
)
def test_native_subcommand_suppresses_inherited_selector_routing(monkeypatch, tool, subcommand):
    monkeypatch.setenv(SMART_ROUTING_CONFIG_VERSION_ENV_VAR, "subagent_orch_v0")
    observed = []

    with patch(
        "ucode.cli._launch_tool",
        side_effect=lambda *_args, **_kwargs: observed.append(
            {
                "selector": os.environ.get(SMART_ROUTING_CONFIG_VERSION_ENV_VAR),
                "v2": os.environ.get(ENABLE_SMART_ROUTING_ENV_VAR),
                "subagent": os.environ.get(ENABLE_SUBAGENT_ROUTING_ENV_VAR),
                "enabled": v2.smart_routing_enabled(),
            }
        ),
    ):
        result = runner.invoke(cli.app, [tool, subcommand])

    assert result.exit_code == 0, result.output
    assert observed == [{"selector": None, "v2": None, "subagent": None, "enabled": False}]
    assert os.environ[SMART_ROUTING_CONFIG_VERSION_ENV_VAR] == "subagent_orch_v0"


def test_session_file_overrides_resolved_selector_for_off_to_on_orchestration(
    tmp_path, monkeypatch
):
    session_path = tmp_path / "session-env.json"
    session_path.write_text("{}", encoding="utf-8")
    environment = {
        SMART_ROUTING_CONFIG_VERSION_ENV_VAR: "subagent_orch_v0",
        session_env.SESSION_ENV_VAR: str(session_path),
    }
    monkeypatch.setenv(session_env.SESSION_ENV_VAR, str(session_path))

    session_env.set_session_environment(
        {
            ENABLE_SMART_ROUTING_ENV_VAR: "0",
            ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0",
        }
    )
    off = session_env.effective_environment(environment)

    assert off[ENABLE_SMART_ROUTING_ENV_VAR] == "0"
    assert off[ENABLE_SUBAGENT_ROUTING_ENV_VAR] == "0"
    assert off[ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR] == "1"
    assert v2.smart_routing_enabled(off) is False
    assert orchestrator.enabled(environment) is False

    session_env.set_session_environment({})
    on = session_env.effective_environment(environment)

    assert on[ENABLE_SMART_ROUTING_ENV_VAR] == "0"
    assert on[ENABLE_SUBAGENT_ROUTING_ENV_VAR] == "1"
    assert on[ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR] == "1"
    assert v2.smart_routing_enabled(on) is True
    assert v2.first_prompt_routing_enabled(on) is False
    assert orchestrator.enabled(environment) is True
    assert environment[SMART_ROUTING_CONFIG_VERSION_ENV_VAR] == "subagent_orch_v0"
