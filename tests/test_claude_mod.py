"""Generated plugin contents for the native Claude mod entry point."""

import json
import sys
from pathlib import Path

import pytest

from ucode.constants import AGENT_CLAUDE, SMART_ROUTING_ENV_KEYS
from ucode.smart_routing import config, orchestrator, savings, v2


def test_plugin_contains_mod_entry_point(tmp_path):
    v2._write_routed_claude_plugin(tmp_path, [])

    assert json.loads((tmp_path / "hooks/hooks.json").read_text()) == {"modules": ["./register.ts"]}
    mods = Path(v2.__file__).parents[1] / "agents" / "claude_mods"
    # The entry point imports these, so they ship beside it unmodified.
    for name in (
        "register.ts",
        "constants.ts",
        "extra-body.ts",
        "smart-routing-status.ts",
        "subagent-routing.ts",
    ):
        assert (tmp_path / "hooks" / name).read_bytes() == (mods / name).read_bytes()
    assert sorted(path.name for path in (tmp_path / "hooks").iterdir()) == [
        "constants.ts",
        "extra-body.ts",
        "hooks.json",
        "register.ts",
        "smart-routing-status.ts",
        "subagent-routing.ts",
    ]


def test_plugin_declares_the_savings_options_at_their_defaults(tmp_path):
    v2._write_routed_claude_plugin(tmp_path, [])

    user_config = json.loads((tmp_path / ".claude-plugin/plugin.json").read_text())["userConfig"]
    assert {
        name: user_config[name]["default"] for name in v2.CLAUDE_SAVINGS_OPTION_DEFAULTS
    } == v2.CLAUDE_SAVINGS_OPTION_DEFAULTS
    # The pricer is an argv list, so paths with spaces reach the mod intact.
    assert user_config["savings_pricer"]["multiple"] is True


@pytest.mark.parametrize("version", [*config._VERSIONS, "", "unsupported_v99"])
def test_launch_options_resolve_versions_without_mutating_input(version):
    env = {
        "SMART_ROUTER_CONFIG_VERSION": version,
        "ENABLE_SMART_ROUTING_V2": "0",
        "ENABLE_SMART_ROUTING_SUBAGENT_ONLY": "0",
        "ENABLE_SMART_ROUTER_ORCHESTRATOR": "0",
        "ENABLE_SMART_ROUTING_SAVINGS": "0",
        "SMART_ROUTER_NAME": " custom_recipe ",
    }
    before = env.copy()
    expected = config._VERSIONS.get(version, {}).get(
        AGENT_CLAUDE, dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")
    )
    assert v2._claude_mod_launch_options(env) == {
        **expected,
        "unset": [],
        "recipe": "custom_recipe",
        "routeCommand": [],
    }
    # Taking the baseline must not alter the caller's launch environment.
    assert env == before


def test_launch_options_preserve_empty_and_absent_values():
    assert v2._claude_mod_launch_options({"ENABLE_SMART_ROUTING_V2": ""}) == {
        "ENABLE_SMART_ROUTING_V2": "",
        "ENABLE_SMART_ROUTING_SUBAGENT_ONLY": "",
        "ENABLE_SMART_ROUTER_ORCHESTRATOR": "",
        "ENABLE_SMART_ROUTING_SAVINGS": "",
        "unset": [
            "ENABLE_SMART_ROUTING_SUBAGENT_ONLY",
            "ENABLE_SMART_ROUTER_ORCHESTRATOR",
            "ENABLE_SMART_ROUTING_SAVINGS",
        ],
        "recipe": "task_v3",
        "routeCommand": [],
    }


@pytest.mark.parametrize("flag", [None, "", "0", "1", "true", "2"])
def test_savings_requires_explicit_one(monkeypatch, flag):
    if flag is not None:
        monkeypatch.setenv(v2.ENABLE_SMART_ROUTING_SAVINGS_ENV_VAR, flag)
    assert v2.savings_enabled() == (flag == "1")


@pytest.mark.parametrize("first_prompt", [False, True])
def test_savings_options_give_the_mod_a_pricer_and_a_launch_local_usage_file(
    tmp_path, monkeypatch, first_prompt
):
    monkeypatch.setenv(v2.ENABLE_SMART_ROUTING_SAVINGS_ENV_VAR, "1")
    price_cache = tmp_path / "model-prices.json"

    assert v2._claude_savings_launch_options(
        tmp_path, price_cache=price_cache, baseline_session_start=first_prompt
    ) == {
        "savings_pricer": savings.pricer_argv(
            python=sys.executable, price_cache=price_cache, baseline_session_start=first_prompt
        ),
        "savings_usage": str(tmp_path / savings.USAGE_FILENAME),
        "orchestrator_version": "",
    }


def test_opting_out_of_savings_leaves_its_options_at_their_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv(v2.ENABLE_SMART_ROUTING_SAVINGS_ENV_VAR, "0")

    assert (
        v2._claude_savings_launch_options(
            tmp_path, price_cache=tmp_path / "model-prices.json", baseline_session_start=False
        )
        == v2.CLAUDE_SAVINGS_OPTION_DEFAULTS
    )


@pytest.mark.parametrize("savings_flag", ["1", "0"])
def test_savings_gates_the_orchestrator_version_too(tmp_path, monkeypatch, savings_flag):
    monkeypatch.setenv("ENABLE_SMART_ROUTER_ORCHESTRATOR", "1")
    monkeypatch.setenv(v2.ENABLE_SMART_ROUTING_SAVINGS_ENV_VAR, savings_flag)

    options = v2._claude_savings_launch_options(
        tmp_path, price_cache=tmp_path / "model-prices.json", baseline_session_start=False
    )

    expected = orchestrator.skill_version(agent=AGENT_CLAUDE) if savings_flag == "1" else ""
    assert options["orchestrator_version"] == expected


def test_native_mod_covers_every_managed_flag():
    source = (Path(v2.__file__).parents[1] / "agents/claude_mods/register.ts").read_text()
    for key in SMART_ROUTING_ENV_KEYS:
        assert f"$.env.get('{key}')" in source
        assert f"$.env.set('{key}', env.{key})" in source
        assert f"{key}: enabled ? original('{key}') : '0'" in source
