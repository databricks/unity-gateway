"""Generated plugin contents for the native Claude mod entry point."""

import json
import re
from pathlib import Path

import pytest

from ucode.constants import AGENT_CLAUDE, SMART_ROUTING_ENV_KEYS
from ucode.smart_routing import config, v2


def test_plugin_contains_mod_entry_point(tmp_path):
    v2._write_routed_claude_plugin(tmp_path, [])

    assert json.loads((tmp_path / "hooks/hooks.json").read_text()) == {"modules": ["./register.ts"]}
    mods = Path(v2.__file__).parents[1] / "agents" / "claude_mods"
    # constants.ts is imported by the entry point, so it ships beside it unmodified.
    for name in ("register.ts", "constants.ts"):
        assert (tmp_path / "hooks" / name).read_bytes() == (mods / name).read_bytes()
    assert sorted(path.name for path in (tmp_path / "hooks").iterdir()) == [
        "constants.ts",
        "hooks.json",
        "register.ts",
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


def test_explicit_toggle_replaces_saved_session_controls():
    """Scenario: the CLI or skill saved a session override before an explicit toggle.

    Expected: the command path asks UG's CLI to rewrite the session controls the hooks
    apply over the environment, while a session start restores only the launch baseline.
    """
    mods = Path(v2.__file__).parents[1] / "agents" / "claude_mods"
    source = (mods / "register.ts").read_text(encoding="utf-8")
    constants = (mods / "constants.ts").read_text(encoding="utf-8")

    # The shared values live in constants.ts; environment names stay literal at their
    # call sites because `plugin validate` rejects a variable there.
    assert "export const ENABLE_ROUTING_FLAG = '--enable-smart-routing';" in constants
    assert "export const DISABLE_ROUTING_FLAG = '--disable-smart-routing';" in constants
    assert not re.search(r"\$\.env\.(get|set)\(", constants)
    # The file stays Python's to write: the CLI validates and replaces it atomically.
    assert re.search(
        r"\$\.process\.run\(\s*\[\s*python,\s*'-m',\s*CLI_MODULE,\s*CLI_AGENT,\s*flag\s*\]\s*\)",
        source,
    )
    assert re.search(r"enabled \? ENABLE_ROUTING_FLAG : DISABLE_ROUTING_FLAG", source)
    # Only an explicit toggle syncs; a session start must not revive a saved off.
    assert re.search(r"action === 'on',\s*true\s*\)", source)
    assert re.search(r"SUBAGENT_ONLY === '1',\s*false\s*\)", source)
