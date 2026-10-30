"""Generated plugin contents for the native Claude mod entry point."""

import json
import shutil
from pathlib import Path

import pytest

from ucode.constants import AGENT_CLAUDE, SMART_ROUTING_ENV_KEYS
from ucode.smart_routing import config, v2


def test_plugin_contains_mod_entry_point(tmp_path):
    v2._write_routed_claude_plugin(tmp_path, [])

    assert json.loads((tmp_path / "hooks/hooks.json").read_text()) == {"modules": ["./register.ts"]}
    mods = Path(v2.__file__).parents[1] / "agents" / "claude_mods"
    # The entry point imports these, so they ship beside it unmodified.
    for name in (
        "register.ts",
        "constants.ts",
        "extra-body.ts",
        "isaac-feedback.ts",
        "routing-state.ts",
        "smart-routing-status.ts",
        "subagent-routing.ts",
    ):
        assert (tmp_path / "hooks" / name).read_bytes() == (mods / name).read_bytes()
    assert sorted(path.name for path in (tmp_path / "hooks").iterdir()) == [
        "constants.ts",
        "extra-body.ts",
        "hooks.json",
        "isaac-feedback.ts",
        "register.ts",
        "routing-state.ts",
        "smart-routing-status.ts",
        "subagent-routing.ts",
    ]


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
    isaac = shutil.which("isaac")
    assert v2._claude_mod_launch_options(env) == {
        **expected,
        "unset": [],
        "recipe": "custom_recipe",
        "routeCommand": [],
        "orchestratorInstructions": "",
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
        "unset": ["ENABLE_SMART_ROUTING_SUBAGENT_ONLY", "ENABLE_SMART_ROUTER_ORCHESTRATOR"],
        "recipe": "stable",
        "routeCommand": [],
        "orchestratorInstructions": "",
        "isaacExecutable": str(Path(isaac).absolute()) if isaac else "",
        "isaacFeedbackDirectory": str(Path.home() / ".isaac" / "feedback"),
    }
