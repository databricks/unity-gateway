"""Generated plugin contents for the native Claude mod entry point."""

import json
import os
import shutil
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from ucode.constants import (
    AGENT_CLAUDE,
    SMART_ROUTER_CONFIG_VERSION_ENV_VAR,
    SMART_ROUTING_ENV_KEYS,
)
from ucode.smart_routing import config, v2


def test_plugin_contains_mod_entry_point(tmp_path):
    v2._write_routed_claude_plugin(tmp_path, [])

    assert json.loads((tmp_path / "hooks/hooks.json").read_text()) == {"modules": ["./register.ts"]}
    manifest = json.loads((tmp_path / ".claude-plugin/plugin.json").read_text())
    assert manifest["userConfig"]["ownershipAnswerDelivery"]["type"] == "boolean"
    assert manifest["userConfig"]["ownershipAnswerDelivery"]["default"] is False
    mods = Path(v2.__file__).parents[1] / "agents" / "claude_mods"
    # The entry point imports these, so they ship beside it unmodified.
    module_names = [
        "register.ts",
        "constants.ts",
        "extra-body.ts",
        "isaac-feedback.ts",
        "routing-state.ts",
        "smart-routing-status.ts",
        "subagent-routing.ts",
    ]
    if (mods / "orchestration-decision.ts").is_file():
        module_names.append("orchestration-decision.ts")
    for name in module_names:
        assert (tmp_path / "hooks" / name).read_bytes() == (mods / name).read_bytes()
    expected_hooks = {
        "constants.ts",
        "extra-body.ts",
        "hooks.json",
        "isaac-feedback.ts",
        "register.ts",
        "routing-state.ts",
        "smart-routing-status.ts",
        "subagent-routing.ts",
    }
    if "orchestration-decision.ts" in module_names:
        expected_hooks.add("orchestration-decision.ts")
    assert {path.name for path in (tmp_path / "hooks").iterdir()} == expected_hooks


def test_claude_mod_configuration_helper_pins_bridge_and_preserves_other_settings(monkeypatch):
    monkeypatch.delenv(v2.CLAUDE_CODE_ENABLE_FUNCTION_HOOKS_ENV_VAR, raising=False)
    route_command = ["/configured/ucode", "routing-hook", "route-subagent"]
    settings = {
        "env": {"caller_setting": "kept"},
        "otherSetting": {"nested": True},
        "pluginConfigs": {
            "other-plugin": {"options": {"setting": "kept"}},
            v2.CLAUDE_ROUTING_PLUGIN_NAME: {
                "options": {"routeCommand": route_command, "customOption": "kept"}
            },
        },
    }
    before = deepcopy(settings)
    env = {SMART_ROUTER_CONFIG_VERSION_ENV_VAR: config.SUBAGENT_ORCH_DECISION_V1_CLAUDE_ONLY}

    v2.configure_claude_mod_settings(
        settings,
        workspace="https://workspace.example",
        env=env,
        answer_delivery=True,
    )

    options = settings["pluginConfigs"][v2.CLAUDE_ROUTING_PLUGIN_NAME]["options"]
    assert options["routeCommand"] == route_command
    assert options["customOption"] == "kept"
    assert options["ownershipCommand"] == [
        os.path.abspath(sys.executable),
        "-m",
        "ucode.smart_routing.ownership",
        "--workspace",
        "https://workspace.example",
    ]
    assert options["ownershipPolicyVersion"] == "orchestration_v1"
    assert options["ownershipAnswerDelivery"] is True
    assert (
        options["ownershipPolicySha256"]
        == "adca6db0480b54494fa76014d72076f020cc4a7a2f66527b54e3671a9980b1fa"
    )
    assert settings["env"] == {
        **before["env"],
        v2.CLAUDE_CODE_ENABLE_FUNCTION_HOOKS_ENV_VAR: "1",
        v2.CLAUDE_CODE_GATEWAY_HINT_HEADERS_ENV_VAR: "1",
    }
    assert settings["otherSetting"] == before["otherSetting"]
    assert settings["pluginConfigs"]["other-plugin"] == before["pluginConfigs"]["other-plugin"]
    expected_outer = deepcopy(before)
    expected_outer["env"][v2.CLAUDE_CODE_ENABLE_FUNCTION_HOOKS_ENV_VAR] = "1"
    expected_outer["env"][v2.CLAUDE_CODE_GATEWAY_HINT_HEADERS_ENV_VAR] = "1"
    actual_outer = deepcopy(settings)
    del expected_outer["pluginConfigs"][v2.CLAUDE_ROUTING_PLUGIN_NAME]["options"]
    del actual_outer["pluginConfigs"][v2.CLAUDE_ROUTING_PLUGIN_NAME]["options"]
    assert actual_outer == expected_outer


@pytest.mark.parametrize("configured_in", ["settings", "launch", "process"])
def test_claude_mod_configuration_preserves_explicit_function_hooks_disable(
    configured_in, monkeypatch
):
    variable = v2.CLAUDE_CODE_ENABLE_FUNCTION_HOOKS_ENV_VAR
    monkeypatch.delenv(variable, raising=False)
    settings = {"env": {}}
    launch_env = {}
    if configured_in == "settings":
        settings["env"][variable] = "0"
    elif configured_in == "launch":
        launch_env[variable] = "0"
    else:
        monkeypatch.setenv(variable, "0")

    with pytest.raises(RuntimeError, match=f"{variable} must be '1'"):
        v2.configure_claude_mod_settings(
            settings, workspace="https://workspace.example", env=launch_env
        )

    if configured_in == "settings":
        assert settings["env"][variable] == "0"
    elif configured_in == "launch":
        assert launch_env[variable] == "0"
    else:
        assert os.environ[variable] == "0"


@pytest.mark.parametrize("configured_in", ["settings", "launch", "process"])
def test_ownership_requires_explicit_gateway_hint_header_gate(configured_in, monkeypatch):
    variable = v2.CLAUDE_CODE_GATEWAY_HINT_HEADERS_ENV_VAR
    monkeypatch.delenv(variable, raising=False)
    monkeypatch.delenv(v2.CLAUDE_CODE_ENABLE_FUNCTION_HOOKS_ENV_VAR, raising=False)
    settings = {"env": {}}
    launch_env = {SMART_ROUTER_CONFIG_VERSION_ENV_VAR: config.SUBAGENT_ORCH_DECISION_V1_CLAUDE_ONLY}
    if configured_in == "settings":
        settings["env"][variable] = "0"
    elif configured_in == "launch":
        launch_env[variable] = "0"
    else:
        monkeypatch.setenv(variable, "0")

    with pytest.raises(RuntimeError, match=f"{variable} must be '1'"):
        v2.configure_claude_mod_settings(
            settings, workspace="https://workspace.example", env=launch_env
        )


def test_legacy_claude_mod_configuration_does_not_add_ownership_bridge(monkeypatch):
    monkeypatch.delenv(v2.CLAUDE_CODE_GATEWAY_HINT_HEADERS_ENV_VAR, raising=False)
    monkeypatch.delenv(v2.CLAUDE_CODE_ENABLE_FUNCTION_HOOKS_ENV_VAR, raising=False)
    settings = {
        "pluginConfigs": {
            v2.CLAUDE_ROUTING_PLUGIN_NAME: {
                "options": {
                    "ownershipCommand": ["stale"],
                    "ownershipPolicyVersion": "stale",
                    "ownershipPolicySha256": "stale",
                    "ownershipAnswerDelivery": True,
                }
            }
        }
    }
    v2.configure_claude_mod_settings(
        settings,
        workspace="https://workspace.example",
        env={SMART_ROUTER_CONFIG_VERSION_ENV_VAR: config.SUBAGENT_ORCH_V1},
    )

    options = settings["pluginConfigs"][v2.CLAUDE_ROUTING_PLUGIN_NAME]["options"]
    assert options["ENABLE_SMART_ROUTER_OWNERSHIP_DECISION"] == "0"
    assert options["routeCommand"] == v2.routing_hook_argv(
        {"workspace": "https://workspace.example", "claude_models": {}},
        v2.ROUTE_SUBAGENT_MOD_EVENT,
    )
    assert "ownershipCommand" not in options
    assert "ownershipPolicyVersion" not in options
    assert "ownershipPolicySha256" not in options
    assert "ownershipAnswerDelivery" not in options
    assert v2.CLAUDE_CODE_GATEWAY_HINT_HEADERS_ENV_VAR not in settings["env"]


def test_ownership_answer_delivery_option_defaults_false(monkeypatch):
    monkeypatch.delenv(v2.CLAUDE_CODE_ENABLE_FUNCTION_HOOKS_ENV_VAR, raising=False)
    monkeypatch.delenv(v2.CLAUDE_CODE_GATEWAY_HINT_HEADERS_ENV_VAR, raising=False)
    settings = {"pluginConfigs": {v2.CLAUDE_ROUTING_PLUGIN_NAME: {"options": {}}}}
    v2.configure_claude_mod_settings(
        settings,
        workspace="https://workspace.example",
        env={SMART_ROUTER_CONFIG_VERSION_ENV_VAR: config.SUBAGENT_ORCH_DECISION_V1_CLAUDE_ONLY},
    )
    assert (
        settings["pluginConfigs"][v2.CLAUDE_ROUTING_PLUGIN_NAME]["options"][
            "ownershipAnswerDelivery"
        ]
        is False
    )
    assert settings["env"][v2.CLAUDE_CODE_GATEWAY_HINT_HEADERS_ENV_VAR] == "1"


@pytest.mark.parametrize("version", [*config._VERSIONS, "", "unsupported_v99"])
def test_launch_options_resolve_versions_without_mutating_input(version):
    env = {
        "SMART_ROUTER_CONFIG_VERSION": version,
        "ENABLE_SMART_ROUTING_V2": "0",
        "ENABLE_SMART_ROUTING_SUBAGENT_ONLY": "0",
        "ENABLE_SMART_ROUTER_ORCHESTRATOR": "0",
        "ENABLE_SMART_ROUTER_OWNERSHIP_DECISION": "0",
        "SMART_ROUTER_NAME": " custom_recipe ",
    }
    before = env.copy()
    expected = config._VERSIONS.get(version, {}).get(
        AGENT_CLAUDE, dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")
    )
    isaac = shutil.which("isaac")
    assert v2._claude_mod_launch_options(env) == {
        **expected,
        "unset": [],
        "recipe": "custom_recipe",
        "routeCommand": [],
        "isaacExecutable": str(Path(isaac).absolute()) if isaac else "",
        "isaacFeedbackDirectory": str(Path.home() / ".isaac" / "feedback"),
    }
    # Taking the baseline must not alter the caller's launch environment.
    assert env == before


def test_launch_options_preserve_empty_and_absent_values():
    isaac = shutil.which("isaac")
    assert v2._claude_mod_launch_options({"ENABLE_SMART_ROUTING_V2": ""}) == {
        "ENABLE_SMART_ROUTING_V2": "",
        "ENABLE_SMART_ROUTING_SUBAGENT_ONLY": "",
        "ENABLE_SMART_ROUTER_ORCHESTRATOR": "",
        "ENABLE_SMART_ROUTER_OWNERSHIP_DECISION": "",
        "unset": [
            "ENABLE_SMART_ROUTING_SUBAGENT_ONLY",
            "ENABLE_SMART_ROUTER_ORCHESTRATOR",
            "ENABLE_SMART_ROUTER_OWNERSHIP_DECISION",
        ],
        "recipe": "task_v3",
        "routeCommand": [],
        "isaacExecutable": str(Path(isaac).absolute()) if isaac else "",
        "isaacFeedbackDirectory": str(Path.home() / ".isaac" / "feedback"),
    }
