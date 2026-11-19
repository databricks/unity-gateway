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
    filenames = (
        "register.ts",
        "auto-model.ts",
        "constants.ts",
        "extra-body.ts",
        "isaac-feedback.ts",
        "routing-logs.ts",
        "routing-state.ts",
        "smart-routing-status.ts",
        "subagent-routing.ts",
    )
    for name in filenames:
        assert (tmp_path / "hooks" / name).read_bytes() == (mods / name).read_bytes()
    assert {path.name for path in (tmp_path / "hooks").iterdir()} == {*filenames, "hooks.json"}
    settings = json.loads((tmp_path / "settings.json").read_text())
    assert settings == {}


def test_regeneration_clears_deprecated_subagent_statusline(tmp_path):
    # A reused generated plugin must stop advertising the old extension too.
    (tmp_path / "settings.json").write_text(
        json.dumps({"subagentStatusLine": {"type": "command", "command": "old-helper"}})
    )
    v2._write_routed_claude_plugin(tmp_path, [])
    assert json.loads((tmp_path / "settings.json").read_text()) == {}


@pytest.mark.parametrize("version", [*config._VERSIONS, "", "unsupported_v99"])
def test_launch_options_resolve_versions_without_mutating_input(version):
    env = {
        "SMART_ROUTER_CONFIG_VERSION": version,
        "ENABLE_SMART_ROUTING_V2": "0",
        "ENABLE_SMART_ROUTING_SUBAGENT_ONLY": "0",
        "ENABLE_SMART_ROUTER_ORCHESTRATOR": "0",
        "ENABLE_SMART_ROUTER_AUTO_MODE": "0",
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
        "logsCommand": [],
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
        "ENABLE_SMART_ROUTER_AUTO_MODE": "0",
        "unset": [
            key
            for key in SMART_ROUTING_ENV_KEYS
            if key not in ("ENABLE_SMART_ROUTING_V2", "ENABLE_SMART_ROUTER_AUTO_MODE")
        ],
        "recipe": "stable",
        "routeCommand": [],
        "logsCommand": [],
        "orchestratorInstructions": "",
        "isaacExecutable": str(Path(isaac).absolute()) if isaac else "",
        "isaacFeedbackDirectory": str(Path.home() / ".isaac" / "feedback"),
    }


@pytest.mark.parametrize("version", [None, "", "unknown_v99", *config._VERSIONS])
def test_mod_auto_flag_is_derived_only_from_version(version):
    env = {"ENABLE_SMART_ROUTER_AUTO_MODE": "1"}
    if version is not None:
        env["SMART_ROUTER_CONFIG_VERSION"] = version
    before = env.copy()
    options = v2._claude_mod_launch_options(env)
    assert options["ENABLE_SMART_ROUTER_AUTO_MODE"] == (
        "1" if version == config.SUBAGENT_AUTO_V0_CLAUDE_ONLY else "0"
    )
    assert "ENABLE_SMART_ROUTER_AUTO_MODE" not in options["unset"]
    assert env == before
