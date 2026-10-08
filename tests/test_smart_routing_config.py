"""Component coverage for versioned smart-routing configuration."""

from __future__ import annotations

import os
import runpy
from itertools import product
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
    SMART_ROUTING_CONFIG_VERSION_ENV_VAR,
    SMART_ROUTING_ENV_KEYS,
)
from ucode.smart_routing import config, orchestrator, session_env, v2

runner = CliRunner()

_SELECTOR_EXPECTED_FLAGS = {
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


def _assert_routing_getters(environment: dict[str, str], expected_flags: dict[str, str]) -> None:
    assert v2.smart_routing_enabled(environment) is True
    assert v2.first_prompt_routing_enabled(environment) is False
    assert orchestrator.feature_enabled(environment) is (
        expected_flags[ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR] == "1"
    )


def test_smart_routing_key_registry_includes_orchestrator_once():
    assert set(SMART_ROUTING_ENV_KEYS) == {
        ENABLE_SMART_ROUTING_ENV_VAR,
        ENABLE_SUBAGENT_ROUTING_ENV_VAR,
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR,
    }
    assert len(SMART_ROUTING_ENV_KEYS) == 3
    assert len(set(SMART_ROUTING_ENV_KEYS)) == len(SMART_ROUTING_ENV_KEYS)
    assert SMART_ROUTING_ENV_KEYS.count(ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR) == 1


@pytest.mark.parametrize(
    ("selector", "legacy_values", "expected_flags"),
    [
        (selector, legacy_values, expected_flags)
        for selector, expected_flags in _SELECTOR_EXPECTED_FLAGS.items()
        for legacy_values in product(("0", "1"), repeat=len(SMART_ROUTING_ENV_KEYS))
    ],
)
def test_selector_precedence_covers_every_legacy_flag_combination(
    selector, legacy_values, expected_flags
):
    legacy_environment = dict(zip(SMART_ROUTING_ENV_KEYS, legacy_values, strict=True))
    original = {
        SMART_ROUTING_CONFIG_VERSION_ENV_VAR: selector,
        **legacy_environment,
        "UNRELATED_SETTING": "preserved",
    }
    expected_environment = {**expected_flags, "UNRELATED_SETTING": "preserved"}

    _assert_routing_getters(original, expected_flags)

    resolved = config.resolve_environment(original)

    assert resolved == expected_environment
    _assert_routing_getters(resolved, expected_flags)
    assert original == {
        SMART_ROUTING_CONFIG_VERSION_ENV_VAR: selector,
        **legacy_environment,
        "UNRELATED_SETTING": "preserved",
    }

    applied = original.copy()
    previous = config.apply_config(applied)

    assert applied == expected_environment
    assert previous == {
        **legacy_environment,
        SMART_ROUTING_CONFIG_VERSION_ENV_VAR: selector,
    }
    _assert_routing_getters(applied, expected_flags)

    v2.restore_smart_routing_env(previous, applied)

    assert applied == original


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


@pytest.mark.parametrize("default", [False, True])
def test_orchestrator_alone_does_not_change_routing_activation_default(default):
    environment = {ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "1"}

    assert v2.smart_routing_enabled(environment, default=default) is default
    assert v2.first_prompt_routing_enabled(environment) is False
    assert orchestrator.feature_enabled(environment) is True


@pytest.mark.parametrize("selector", ["future_mode", "subagent_orch_v1"])
@pytest.mark.parametrize("resolver", [config.resolve_environment, config.apply_config])
def test_unknown_selectors_remain_unknown(selector, resolver):
    environment = {SMART_ROUTING_CONFIG_VERSION_ENV_VAR: selector}

    with pytest.raises(RuntimeError) as caught:
        resolver(environment)

    assert selector in str(caught.value)
    assert environment == {SMART_ROUTING_CONFIG_VERSION_ENV_VAR: selector}


@pytest.mark.parametrize("selector", [None, "", " "])
def test_apply_config_is_a_noop_for_unset_or_blank_selector(selector):
    environment = {"UNRELATED_SETTING": "preserved"}
    if selector is not None:
        environment[SMART_ROUTING_CONFIG_VERSION_ENV_VAR] = selector
    original = environment.copy()

    assert config.apply_config(environment) == {}
    assert environment == original


def test_unknown_selector_mentions_supported_names():
    for resolver in (config.resolve_environment, config.apply_config):
        environment = {SMART_ROUTING_CONFIG_VERSION_ENV_VAR: "future_mode"}

        with pytest.raises(RuntimeError) as caught:
            resolver(environment)

        message = str(caught.value)
        assert "future_mode" in message
        assert "subagent_only_v0" in message
        assert "subagent_only_v1" in message
        assert "subagent_orch_v0" in message
        assert environment == {SMART_ROUTING_CONFIG_VERSION_ENV_VAR: "future_mode"}


@pytest.mark.parametrize("missing_key", SMART_ROUTING_ENV_KEYS)
def test_validate_versions_rejects_each_missing_managed_flag(missing_key):
    values = {key: "0" for key in SMART_ROUTING_ENV_KEYS if key != missing_key}

    with pytest.raises(ValueError):
        config._validate_versions({"test_version": values})


@pytest.mark.parametrize("invalid_value", [None, "", "2", "true", 0, False])
def test_validate_versions_rejects_non_binary_managed_flag_values(invalid_value):
    values = dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")
    values[ENABLE_SMART_ROUTING_ENV_VAR] = invalid_value

    with pytest.raises(ValueError):
        config._validate_versions({"test_version": values})


def test_validate_versions_rejects_unexpected_managed_flag():
    values = dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")
    values["UNEXPECTED_SMART_ROUTING_FLAG"] = "0"

    with pytest.raises(ValueError):
        config._validate_versions({"test_version": values})


def test_config_import_rejects_new_registry_flag_before_runtime_use(monkeypatch):
    new_key = "ENABLE_SMART_ROUTING_TEST_ONLY"
    monkeypatch.setattr(
        constants,
        "SMART_ROUTING_ENV_KEYS",
        (*constants.SMART_ROUTING_ENV_KEYS, new_key),
    )

    with pytest.raises(ValueError):
        runpy.run_path(config.__file__)


@pytest.mark.parametrize("operation", ["enable", "override", "disable"])
def test_v2_routing_toggles_restore_selector_and_legacy_environment(operation):
    original = {
        SMART_ROUTING_CONFIG_VERSION_ENV_VAR: "subagent_orch_v0",
        ENABLE_SMART_ROUTING_ENV_VAR: "old-v2",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "old-subagent",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "old-orchestrator",
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
    assert previous[ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR] == "old-orchestrator"

    v2.restore_smart_routing_env(previous, environment)

    assert environment == original


def test_explicit_disable_wins_over_selector_until_restored():
    original = {SMART_ROUTING_CONFIG_VERSION_ENV_VAR: "subagent_orch_v0"}
    environment = original.copy()

    previous = v2.override_smart_routing(False, environment)

    assert SMART_ROUTING_CONFIG_VERSION_ENV_VAR not in environment
    assert environment[ENABLE_SMART_ROUTING_ENV_VAR] == "0"
    assert environment[ENABLE_SUBAGENT_ROUTING_ENV_VAR] == "0"
    assert environment[ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR] == "0"
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


def test_cli_startup_resolves_selector_before_command_callbacks(monkeypatch):
    selector = "subagent_only_v1"
    expected_flags = _SELECTOR_EXPECTED_FLAGS[selector]
    original = {
        SMART_ROUTING_CONFIG_VERSION_ENV_VAR: selector,
        ENABLE_SMART_ROUTING_ENV_VAR: "0",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "1",
    }
    for key, value in original.items():
        monkeypatch.setenv(key, value)
    observed = []

    def record_callback(name):
        observed.append(
            {
                "callback": name,
                "selector": os.environ.get(SMART_ROUTING_CONFIG_VERSION_ENV_VAR),
                "flags": {key: os.environ.get(key) for key in SMART_ROUTING_ENV_KEYS},
            }
        )

    def record_session_toggle(enabled):
        assert enabled is None
        record_callback("session_toggle")
        return False

    def record_custom_oauth(*_arguments, **_options):
        record_callback("custom_oauth")
        return None

    def record_launch(*_arguments, **_options):
        record_callback("launch")

    with (
        patch.object(
            cli,
            "_toggle_current_smart_routing_session",
            side_effect=record_session_toggle,
        ),
        patch.object(cli, "_custom_oauth_config", side_effect=record_custom_oauth),
        patch.object(cli, "_launch_tool", side_effect=record_launch),
    ):
        result = runner.invoke(cli.app, ["claude", "--client-id", "client"])

    assert result.exit_code == 0, result.output
    assert observed == [
        {"callback": "session_toggle", "selector": None, "flags": expected_flags},
        {"callback": "custom_oauth", "selector": None, "flags": expected_flags},
        {"callback": "launch", "selector": None, "flags": expected_flags},
    ]
    assert {key: os.environ.get(key) for key in original} == original


@pytest.mark.parametrize(
    "arguments",
    [[], ["--version"], ["configure"], ["claude", "--enable-smart-routing"]],
    ids=["bare-launch", "version", "configure", "session-toggle"],
)
def test_cli_startup_rejects_invalid_selector_before_callbacks(monkeypatch, arguments):
    original = {
        SMART_ROUTING_CONFIG_VERSION_ENV_VAR: "future_mode",
        ENABLE_SMART_ROUTING_ENV_VAR: "1",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "1",
    }
    for key, value in original.items():
        monkeypatch.setenv(key, value)

    with (
        patch("ucode.telemetry.ug_version") as version,
        patch.object(cli, "_custom_oauth_config") as custom_oauth,
        patch.object(cli, "_toggle_current_smart_routing_session") as session_toggle,
        patch.object(cli, "configure_workspace_command") as configure_workspace,
        patch.object(cli, "install_databricks_cli") as install_cli,
        patch.object(cli, "_launch_managed_default") as launch_managed_default,
    ):
        result = runner.invoke(cli.app, arguments)

    assert result.exit_code == 1, result.output
    assert "Unknown SMART_ROUTING_CONFIG_VERSION" in result.output
    for supported_selector in _SELECTOR_EXPECTED_FLAGS:
        assert supported_selector in result.output
    version.assert_not_called()
    custom_oauth.assert_not_called()
    session_toggle.assert_not_called()
    configure_workspace.assert_not_called()
    install_cli.assert_not_called()
    launch_managed_default.assert_not_called()
    assert {key: os.environ.get(key) for key in original} == original


@pytest.mark.parametrize(
    ("exit_case", "arguments", "expected_exit_code"),
    [
        ("version", ["--version"], 0),
        ("command-error", ["claude"], 1),
        ("parse-error", ["--workspace"], 2),
    ],
)
def test_cli_startup_restores_parent_environment_after_exit(
    monkeypatch, exit_case, arguments, expected_exit_code
):
    original = {
        SMART_ROUTING_CONFIG_VERSION_ENV_VAR: "subagent_only_v1",
        ENABLE_SMART_ROUTING_ENV_VAR: "0",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "1",
    }
    for key, value in original.items():
        monkeypatch.setenv(key, value)
    launch_error = RuntimeError("launch failed") if exit_case == "command-error" else None

    with patch.object(cli, "_launch_tool", side_effect=launch_error) as launch:
        result = runner.invoke(cli.app, arguments)

    assert result.exit_code == expected_exit_code, result.output
    assert {key: os.environ.get(key) for key in original} == original
    if exit_case in {"version", "parse-error"}:
        launch.assert_not_called()
    else:
        launch.assert_called_once()


@pytest.mark.parametrize(
    ("flag", "expected_flags"),
    [
        (
            "--enable-smart-routing",
            {
                ENABLE_SMART_ROUTING_ENV_VAR: "1",
                ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
                ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "0",
            },
        ),
        (
            "--disable-smart-routing",
            {
                ENABLE_SMART_ROUTING_ENV_VAR: "0",
                ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0",
                ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "0",
            },
        ),
    ],
)
def test_cli_explicit_routing_control_overrides_resolved_selector(
    monkeypatch, flag, expected_flags
):
    original = {
        SMART_ROUTING_CONFIG_VERSION_ENV_VAR: "subagent_only_v0",
        ENABLE_SMART_ROUTING_ENV_VAR: "1",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "1",
    }
    for key, value in original.items():
        monkeypatch.setenv(key, value)
    observed = []

    def record_launch(*_arguments, **_options):
        observed.append(
            {
                SMART_ROUTING_CONFIG_VERSION_ENV_VAR: os.environ.get(
                    SMART_ROUTING_CONFIG_VERSION_ENV_VAR
                ),
                **{key: os.environ.get(key) for key in SMART_ROUTING_ENV_KEYS},
            }
        )

    with patch.object(cli, "_launch_tool", side_effect=record_launch):
        result = runner.invoke(cli.app, ["codex", flag])

    assert result.exit_code == 0, result.output
    assert observed == [{SMART_ROUTING_CONFIG_VERSION_ENV_VAR: None, **expected_flags}]
    assert {key: os.environ.get(key) for key in original} == original


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


def test_managed_routing_preserves_subagent_only_v0_flags_during_full_launch(monkeypatch):
    original = {
        SMART_ROUTING_CONFIG_VERSION_ENV_VAR: "subagent_only_v0",
        ENABLE_SMART_ROUTING_ENV_VAR: "1",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "1",
    }
    for key, value in original.items():
        monkeypatch.setenv(key, value)
    launch_state = {
        "workspace": "https://example.databricks.com",
        "available_tools": ["claude"],
        "base_urls": {"claude": "https://example.databricks.com/ai-gateway/anthropic"},
        "claude_models": {"sonnet": "databricks-claude-sonnet-4"},
        "managed_configs": {},
    }
    managed = {"enabled_agents": {"claude": {"smart_routing_enabled": True}}}
    observed = []

    def record_launch(*_arguments, **_options):
        observed.append({key: os.environ.get(key) for key in SMART_ROUTING_ENV_KEYS})

    with (
        patch.object(cli, "ensure_bootstrap_dependencies"),
        patch.object(cli, "load_state", return_value=launch_state),
        patch.object(cli, "ensure_provider_state", return_value=launch_state),
        patch.object(cli, "_fetch_managed_config", return_value=(managed, False)),
        patch.object(cli, "_fetch_budget_recommendation", return_value=None),
        patch.object(cli, "get_provider_service", return_value=None),
        patch.object(cli, "configure_shared_state", return_value=launch_state),
        patch.object(
            cli,
            "resolve_launch_model",
            return_value=(launch_state, "databricks-claude-sonnet-4"),
        ),
        patch.object(cli, "configure_tool", return_value=launch_state),
        patch.object(cli, "refresh_downloaded_skills_on_launch"),
        patch.object(cli, "launch_agent", side_effect=record_launch) as launch,
    ):
        result = runner.invoke(cli.app, ["claude"])

    assert result.exit_code == 0, result.output
    launch.assert_called_once()
    assert observed == [
        {
            ENABLE_SMART_ROUTING_ENV_VAR: "0",
            ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
            ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "0",
        }
    ]
    assert {key: os.environ.get(key) for key in original} == original


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
