"""Generated plugin contents for the native Claude mod entry point."""

import json
import shlex
import shutil
import subprocess
import sys
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
        "subagent-statusline.py",
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
        "subagent-statusline.py",
    ]
    settings = json.loads((tmp_path / "settings.json").read_text())
    assert settings == {
        "subagentStatusLine": {
            "type": "command",
            "command": v2.subprocess_cross_os.shell_command(
                [sys.executable, str(tmp_path / "hooks/subagent-statusline.py")]
            ),
        }
    }


def test_native_subagent_statusline_command(tmp_path):
    plugin = tmp_path / "plugin with spaces"
    v2._write_routed_claude_plugin(plugin, [])
    names = plugin / "agent-names"
    names.mkdir()
    (names / "agent-child-a.json").write_text('{"session_id":"s1","name":"Otter"}')
    (names / "agent-child-b.json").write_text('{"session_id":"s1","name":"Lynx 2"}')
    (names / "agent-other-session.json").write_text('{"session_id":"s2","name":"Fox"}')
    (names / "agent-partial.json").write_text('{"session_id":')
    command = json.loads((plugin / "settings.json").read_text())["subagentStatusLine"]["command"]
    if sys.platform != "win32":
        assert shlex.split(command) == [
            sys.executable,
            str(plugin / "hooks/subagent-statusline.py"),
        ]

    def render(payload):
        result = subprocess.run(
            command,
            shell=True,
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=5,
        )
        assert result.returncode == 0, result.stderr
        return [json.loads(line) for line in result.stdout.splitlines()]

    task = {
        "type": "local_agent",
        "agentType": "ug-smart-router:ucode-route-sonnet-hash",
        "status": "running",
        "description": "Inspect parser",
        "label": "Reading files",
        "tokenCount": 1234,
        "startTime": 9999999999999,
    }
    payload = {
        "session_id": "s1",
        "tasks": [
            {**task, "id": "child-b", "status": "completed", "label": ""},
            {**task, "id": "child-a"},
            *({**task, "id": id} for id in ("missing", "other-session", "partial")),
            {**task, "id": "child-a", "type": "local_bash"},
            None,
        ],
    }
    assert render(payload) == [
        {"id": "child-b", "content": "Lynx 2 · Inspect parser · 1,234 tokens"},
        {"id": "child-a", "content": "Otter · Reading files · 1,234 tokens · 0s"},
    ]
    payload["tasks"] = [
        {**task, "id": "child-a", "label": "Latest\nprogress\x1b[31m", "tokenCount": 2345}
    ]
    assert render(payload) == [
        {
            "id": "child-a",
            "content": "Otter · Latestprogress[31m · 2,345 tokens · 0s",
        }
    ]
    for invalid in (None, [], {}, {"session_id": "s1", "tasks": None}):
        assert render(invalid) == []


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
