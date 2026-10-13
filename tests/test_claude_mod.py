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
    # constants.ts is imported by the entry point, so it ships beside it unmodified.
    for name in ("register.ts", "constants.ts", "savings.ts"):
        assert (tmp_path / "hooks" / name).read_bytes() == (mods / name).read_bytes()
    assert sorted(path.name for path in (tmp_path / "hooks").iterdir()) == [
        "constants.ts",
        "hooks.json",
        "register.ts",
        "savings.ts",
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
    }
    # Taking the baseline must not alter the caller's launch environment.
    assert env == before


def test_launch_options_preserve_empty_and_absent_values():
    assert v2._claude_mod_launch_options({"ENABLE_SMART_ROUTING_V2": ""}) == {
        "ENABLE_SMART_ROUTING_V2": "",
        "ENABLE_SMART_ROUTING_SUBAGENT_ONLY": "",
        "ENABLE_SMART_ROUTER_ORCHESTRATOR": "",
        "unset": ["ENABLE_SMART_ROUTING_SUBAGENT_ONLY", "ENABLE_SMART_ROUTER_ORCHESTRATOR"],
        "recipe": "task_v3",
    }


def test_savings_is_on_by_default_and_opts_out_with_zero(monkeypatch):
    assert v2.savings_enabled()
    monkeypatch.setenv(v2.ENABLE_SMART_ROUTING_SAVINGS_ENV_VAR, "0")
    assert not v2.savings_enabled()
    monkeypatch.setenv(v2.ENABLE_SMART_ROUTING_SAVINGS_ENV_VAR, "1")
    assert v2.savings_enabled()


@pytest.mark.parametrize("first_prompt", [False, True])
def test_savings_options_give_the_mod_a_pricer_and_a_launch_local_usage_file(
    tmp_path, first_prompt
):
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
def test_savings_options_show_the_orchestrator_skill_version_when_it_is_active(
    tmp_path, monkeypatch, savings_flag
):
    monkeypatch.setenv("ENABLE_SMART_ROUTER_ORCHESTRATOR", "1")
    monkeypatch.setenv(v2.ENABLE_SMART_ROUTING_SAVINGS_ENV_VAR, savings_flag)

    options = v2._claude_savings_launch_options(
        tmp_path, price_cache=tmp_path / "model-prices.json", baseline_session_start=False
    )

    assert options["orchestrator_version"] == orchestrator.skill_version(agent=AGENT_CLAUDE)
    assert options["orchestrator_version"]
