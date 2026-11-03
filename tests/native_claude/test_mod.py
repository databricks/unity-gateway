"""Validate and exercise the mod entry point using an explicitly selected Claude."""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest
from messages_api import messages_api
from session import Session

from ucode.agents.claude import _header_mod_files
from ucode.smart_routing import config, orchestrator, savings, session_env
from ucode.smart_routing.v2 import (
    CLAUDE_SAVINGS_OPTION_DEFAULTS,
    _claude_mod_launch_options,
    _write_routed_claude_plugin,
)


def _savings_options(root: Path, *, first_prompt: bool = False) -> dict:
    return {
        # A space and a non-ASCII character check the argv survives the options round trip.
        "savings_pricer": savings.pricer_argv(
            python=sys.executable,
            price_cache=root / "no prices λ.json",
            baseline_session_start=first_prompt,
        ),
        "savings_usage": str(root / savings.USAGE_FILENAME),
        "orchestrator_version": "1.2.0",
    }


def _recorded_usage(path: Path, entry_count: int, *, output_tokens: int = 0) -> dict:
    deadline = time.monotonic() + 10
    recorded = None
    while time.monotonic() < deadline:
        try:
            recorded = json.loads(path.read_text(encoding="utf-8"))
            if (
                len(recorded["entries"]) == entry_count
                and sum(entry["usage"]["output_tokens"] for entry in recorded["entries"])
                == output_tokens
            ):
                return recorded
        except (OSError, ValueError):
            pass
        time.sleep(0.05)
    pytest.fail(f"savings did not record {entry_count} entries: {recorded}")


def test_mod_validation_and_event_forwarding(native_binary, tmp_path):
    path = tmp_path / "plugin"
    _write_routed_claude_plugin(path, [])
    manifest = path / ".claude-plugin/plugin.json"
    doc = json.loads(manifest.read_text())
    doc["userConfig"]["routeCommand"]["default"] = [
        "/bin/ug",
        "claude-router-hook",
        "route-subagent-mod",
    ]
    manifest.write_text(json.dumps(doc))
    shutil.copytree(Path(__file__).with_name("mod_tests"), path / "tests")
    for command in ("validate", "test"):
        result = subprocess.run(
            [native_binary, "plugin", command, str(path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        if command == "validate":
            hooks = next(
                line for line in result.stdout.splitlines() if "./register.ts hooks:" in line
            )
            assert "session.start" in hooks


def test_header_mod_validation_and_merge(native_binary, tmp_path):
    for path, text in _header_mod_files(tmp_path).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    plugin = tmp_path / "ug-headers"
    shutil.copytree(Path(__file__).with_name("header_mod_tests"), plugin / "tests")
    for command in ("validate", "test"):
        result = subprocess.run(
            [native_binary, "plugin", command, str(plugin)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        if command == "validate":
            assert "env writes: ANTHROPIC_CUSTOM_HEADERS" in result.stdout


def test_native_options_are_launch_local(native_binary, tmp_path):
    env = {
        "SMART_ROUTER_CONFIG_VERSION": config.SUBAGENT_ORCH_SAVINGS_V0,
        "SMART_ROUTER_NAME": 'custom "quoted" λ',
    }
    savings_options = _savings_options(tmp_path / "a")
    with (
        Session(native_binary, tmp_path / "a", env, options=savings_options) as a,
        Session(native_binary, tmp_path / "b", {}) as b,
    ):
        assert a.observe()["options"] == {**_claude_mod_launch_options(env), **savings_options}
        # Claude fills in the declared defaults for options a launch omits.
        assert b.observe()["options"] == {
            **_claude_mod_launch_options({}),
            **CLAUDE_SAVINGS_OPTION_DEFAULTS,
        }
        assert not (a.plugin / "hooks/launch.json").exists()


def test_savings_mod_records_the_start_model(native_binary, tmp_path):
    root = tmp_path / "session"
    options = _savings_options(root)
    usage = Path(options["savings_usage"])
    with Session(
        native_binary, root, {"ENABLE_SMART_ROUTING_SAVINGS": "1"}, options=options
    ) as session:
        session.observe()
        # The mod writes its sums off the session.start critical path.
        deadline = time.monotonic() + 10
        while True:
            try:
                recorded = json.loads(usage.read_text(encoding="utf-8"))
                break
            except (OSError, ValueError):
                assert time.monotonic() < deadline, "the savings mod never wrote its usage file"
                time.sleep(0.1)

    assert recorded == {
        "version": 1,
        "start_model": "claude-sonnet-4-6",
        "main_model": None,
        "first_main_model": None,
        "first_prompt_seen": False,
        "user_switched": False,
        "entries": [],
    }


@pytest.mark.parametrize(
    ("alias", "family", "model"),
    [
        ("sonnet", "SONNET", "system.ai.claude-sonnet-4-6"),
        ("opus[1m]", "OPUS", "system.ai.claude-opus-4-8"),
    ],
)
def test_savings_mod_resolves_the_starting_alias(native_binary, tmp_path, alias, family, model):
    root = tmp_path / "session"
    options = _savings_options(root, first_prompt=True)
    with Session(
        native_binary,
        root,
        {
            "ENABLE_SMART_ROUTING_SAVINGS": "1",
            f"ANTHROPIC_DEFAULT_{family}_MODEL": model,
        },
        model=alias,
        options=options,
    ) as session:
        session.observe()
        recorded = _recorded_usage(Path(options["savings_usage"]), 0)

    assert recorded["start_model"] == model


def test_savings_mod_tracks_native_main_and_subagent_usage(native_binary, tmp_path):
    """Real Claude executes Agent and parses controlled API usage for every turn step."""
    root = tmp_path / "session"
    options = _savings_options(root)
    main = "claude-sonnet-4-6"
    child = "claude-haiku-4-5"
    counts = [
        {
            "input_tokens": 11,
            "output_tokens": 13,
            "cache_creation_input_tokens": 17,
            "cache_read_input_tokens": 19,
        },
        {
            "input_tokens": 23,
            "output_tokens": 29,
            "cache_creation_input_tokens": 31,
            "cache_read_input_tokens": 37,
        },
        {
            "input_tokens": 41,
            "output_tokens": 43,
            "cache_creation_input_tokens": 47,
            "cache_read_input_tokens": 53,
        },
    ]
    replies = [
        {
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_fixture_child",
                    "name": "Agent",
                    "input": {
                        "description": "Read the child assignment",
                        "prompt": "Reply with child done.",
                        "subagent_type": "general-purpose",
                        "model": "haiku",
                        "run_in_background": False,
                    },
                }
            ],
            "usage": counts[0],
        },
        {
            "content": [{"type": "text", "text": "child done"}],
            "usage": counts[1],
            # The API's deployment spelling must not replace the requested billing model.
            "model": "anthropic.claude-haiku-4-5-20251001-v1:0",
        },
        {"content": [{"type": "text", "text": "parent done"}], "usage": counts[2]},
    ]
    with (
        messages_api(replies) as (api, requests),
        Session(
            native_binary,
            root,
            {"ENABLE_SMART_ROUTING_SAVINGS": "1", "ANTHROPIC_DEFAULT_HAIKU_MODEL": child},
            api=api,
            options=options,
        ) as session,
    ):
        result = session.send("Delegate the child assignment, then say parent done.")
        assert result["result"] == "parent done"
        recorded = _recorded_usage(Path(options["savings_usage"]), 2, output_tokens=85)

    assert [request["model"] for request in requests] == [main, child, main]
    assert "child done" in json.dumps(requests[2]["messages"])
    entries = recorded["entries"]
    parent_entry = next(entry for entry in entries if entry["agent"] == "main")
    child_entry = next(entry for entry in entries if entry["agent"] != "main")
    assert parent_entry == {
        "agent": "main",
        "baseline": main,
        "served": main,
        "before_user_switch": True,
        "usage": {
            "input_tokens": 52,
            "output_tokens": 56,
            "cache_creation_input_tokens": 64,
            "cache_read_input_tokens": 72,
        },
    }
    assert child_entry == {
        "agent": child_entry["agent"],
        "baseline": main,
        "served": child,
        "before_user_switch": True,
        "usage": counts[1],
    }
    assert recorded["start_model"] == recorded["main_model"] == main
    assert recorded["user_switched"] is False


def test_savings_baseline_uses_manual_model_before_first_prompt(native_binary, tmp_path):
    root = tmp_path / "session"
    options = _savings_options(root, first_prompt=True)
    selected = "claude-haiku-4-5"
    replies = [
        {
            "content": [{"type": "text", "text": "done"}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    ]
    with (
        messages_api(replies) as (api, requests),
        Session(
            native_binary,
            root,
            {"ENABLE_SMART_ROUTING_SAVINGS": "1", "ANTHROPIC_DEFAULT_HAIKU_MODEL": selected},
            api=api,
            options=options,
        ) as session,
    ):
        session.send("/model haiku")
        assert session.send("Reply with done.")["result"] == "done"
        recorded = _recorded_usage(Path(options["savings_usage"]), 1, output_tokens=5)

    assert [request["model"] for request in requests] == [selected]
    assert recorded["start_model"] == selected
    assert recorded["entries"][0]["baseline"] == selected
    assert recorded["entries"][0]["served"] == selected


def test_savings_baseline_survives_routed_replay_and_later_model_change(native_binary, tmp_path):
    """Exercise the native block/model/replay sequence used by the PTY, then a user switch."""
    root = tmp_path / "session"
    options = _savings_options(root, first_prompt=True)
    blocked = tmp_path / "blocked"
    hook = tmp_path / "block_first.py"
    hook.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "payload = json.load(sys.stdin)\n"
        "marker = Path(sys.argv[1])\n"
        "if not payload['prompt'].lstrip().startswith('/') and not marker.exists():\n"
        "    marker.write_text('blocked')\n"
        "    print(json.dumps({'decision': 'block', 'reason': 'First prompt captured'}))\n"
    )
    selected, routed, later = "claude-haiku-4-5", "claude-opus-4-8", "claude-sonnet-4-6"
    replies = [
        {
            "content": [{"type": "text", "text": "done"}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
        {
            "content": [{"type": "text", "text": "second done"}],
            "usage": {"input_tokens": 20, "output_tokens": 7},
        },
    ]
    with (
        messages_api(replies) as (api, requests),
        Session(
            native_binary,
            root,
            {
                "ENABLE_SMART_ROUTING_SAVINGS": "1",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": selected,
                "ANTHROPIC_DEFAULT_OPUS_MODEL": routed,
                "ANTHROPIC_DEFAULT_SONNET_MODEL": later,
            },
            api=api,
            options=options,
            hooks={
                "UserPromptSubmit": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": shlex.join([sys.executable, str(hook), str(blocked)]),
                            }
                        ]
                    }
                ]
            },
        ) as session,
    ):
        session.send("/model haiku")
        blocked_result = session.send("Reply with done.")
        assert blocked_result["num_turns"] == 0
        assert "First prompt captured" in blocked_result["result"]
        assert blocked.read_text() == "blocked"
        assert requests == []
        # The PTY sends this native command after the hook has blocked the prompt.
        session.send("/model opus")
        assert session.send("Reply with done.")["result"] == "done"
        first = _recorded_usage(Path(options["savings_usage"]), 1, output_tokens=5)
        session.send("/model sonnet")
        assert session.send("Now reply with second done.")["result"] == "second done"
        recorded = _recorded_usage(Path(options["savings_usage"]), 2, output_tokens=12)

    assert [request["model"] for request in requests] == [routed, later]
    assert first["start_model"] == recorded["start_model"] == selected
    assert recorded["first_main_model"] == routed
    assert recorded["first_prompt_seen"] is True
    assert recorded["user_switched"] is True
    earlier = next(entry for entry in recorded["entries"] if entry["served"] == routed)
    assert first["entries"] == [earlier]
    assert earlier["before_user_switch"] is True
    current = next(entry for entry in recorded["entries"] if entry["served"] == later)
    assert current["before_user_switch"] is False
    assert current["baseline"] == later


def test_savings_tracking_obeys_cli_session_controls(native_binary, tmp_path):
    root = tmp_path / "session"
    options = _savings_options(root)
    env = {
        "ENABLE_SMART_ROUTING_V2": "0",
        "ENABLE_SMART_ROUTING_SUBAGENT_ONLY": "1",
        "ENABLE_SMART_ROUTER_ORCHESTRATOR": "0",
        "ENABLE_SMART_ROUTING_SAVINGS": "1",
        "PYTHONPATH": str(Path(__file__).parents[2] / "src"),
    }
    controls = session_env.start_session(env)
    replies = [
        {"content": [{"type": "text", "text": "done"}], "usage": usage}
        for usage in (
            {"input_tokens": 10, "output_tokens": 5},
            {"input_tokens": 20, "output_tokens": 7},
            {"input_tokens": 30, "output_tokens": 11},
        )
    ]
    try:
        with (
            messages_api(replies) as (api, requests),
            Session(native_binary, root, env, api=api, options=options) as session,
        ):
            assert session.send("Reply with done.")["result"] == "done"
            _recorded_usage(Path(options["savings_usage"]), 1, output_tokens=5)
            for action in ("disable", "enable"):
                result = subprocess.run(
                    [sys.executable, "-m", "ucode.cli", "claude", f"--{action}-smart-routing"],
                    env=env,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                assert result.returncode == 0, result.stderr
                saved_controls = json.loads(controls.read_text())
                if action == "disable":
                    assert saved_controls["ENABLE_SMART_ROUTING_SAVINGS"] == "0"
                else:
                    assert saved_controls == {}
                assert session.send("Reply with done again.")["result"] == "done"
            recorded = _recorded_usage(Path(options["savings_usage"]), 1, output_tokens=16)
        assert len(requests) == 3
        assert recorded["entries"][0]["usage"] == {
            "input_tokens": 40,
            "output_tokens": 16,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
    finally:
        shutil.rmtree(controls.parent)


@pytest.mark.parametrize("flag", [None, "", "0"])
def test_disabled_savings_does_not_create_usage_file(native_binary, tmp_path, flag):
    root = tmp_path / "session"
    options = _savings_options(root)
    env = {} if flag is None else {"ENABLE_SMART_ROUTING_SAVINGS": flag}
    # Stale savings options must not enable the feature without an explicit flag.
    with Session(native_binary, root, env, options=options) as session:
        session.observe()
    assert not Path(options["savings_usage"]).exists()


def test_orchestrator_instructions_follow_native_toggles(native_binary, tmp_path):
    """Native and CLI toggles update request guidance without changing the Agent schema.

    Inference deliberately fails without credentials; this checks the serialized request,
    not a model response or delegation quality.
    """

    def agent_tool(session):
        session.send("Reply with OK.", expect_error=True)
        records = [
            json.loads(line)
            for path in session.root.rglob("*.jsonl")
            for line in path.read_text().splitlines()
        ]
        shapes = {
            record["shapeHash"]: record["shape"]
            for record in records
            if record.get("type") == "api-request-shape"
        }
        requests = [record for record in records if record.get("type") == "api-request"]
        assert requests, "Claude did not record a serialized API request"
        tools = shapes[requests[-1]["shapeHash"]]["tools"]
        return next(tool for tool in tools if tool["name"] == "Agent")

    env = {
        "ENABLE_SMART_ROUTING_V2": "0",
        "ENABLE_SMART_ROUTING_SUBAGENT_ONLY": "1",
        "ENABLE_SMART_ROUTER_ORCHESTRATOR": "1",
        "PYTHONPATH": str(Path(__file__).parents[2] / "src"),
    }
    controls = session_env.start_session(env)
    try:
        with Session(
            native_binary,
            tmp_path / "no-orchestrator",
            {**env, "ENABLE_SMART_ROUTER_ORCHESTRATOR": "0"},
        ) as session:
            native_tool = agent_tool(session)
        assert native_tool["description"] != orchestrator.CLAUDE_AGENT_DESCRIPTION
        with Session(native_binary, tmp_path / "routed", env) as session:
            routed_tool = agent_tool(session)
            assert {**routed_tool, "description": native_tool["description"]} == native_tool
            description = routed_tool["description"]
            assert description.startswith(orchestrator.CLAUDE_AGENT_DESCRIPTION)
            assert "notification in a later turn" in description
            assert "end the current turn and wait for that notification" in description
            assert "Agents run in the background by default." in description
            assert "do NOT sleep, poll, or proactively check on its progress" in description
            assert "starts a fresh agent with no memory of prior runs" in description
            for policy in (
                "## When not to use",
                "## When to fork",
                "Never delegate understanding",
                "When in doubt, don't spawn",
            ):
                assert policy not in description
            for action in ("off", "on"):
                result = session.send(f"/smart-router {action}")
                assert f"Smart Router is {action}" in result["result"]
                assert agent_tool(session) == (routed_tool if action == "on" else native_tool)
                saved = json.loads(controls.read_text())
                if action == "on":
                    assert saved == {}
                else:
                    assert saved["ENABLE_SMART_ROUTER_ORCHESTRATOR"] == "0"
            for action, flag in (
                ("off", "--disable-smart-routing"),
                ("on", "--enable-smart-routing"),
            ):
                result = subprocess.run(
                    [sys.executable, "-m", "ucode.cli", "claude", flag],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                assert result.returncode == 0, result.stderr
                assert f"Smart Router is {action}" in result.stdout
                assert agent_tool(session) == (routed_tool if action == "on" else native_tool)
        transcript = next(session.root.glob("config/projects/*/*.jsonl"))
        records = [json.loads(line) for line in transcript.read_text().splitlines()]
        last = next(record for record in reversed(records) if record.get("type") == "assistant")
        # Successful inference records this attachment. Seed that resume input without
        # a model service; the real host must ignore its stale description after toggling.
        snapshot = {
            "type": "attachment",
            "uuid": str(uuid4()),
            "parentUuid": last["parentUuid"],
            "sessionId": last["sessionId"],
            "timestamp": last["timestamp"],
            "attachment": {
                "type": "prompt_snapshot",
                "systemPrompt": ["Saved session prompt."],
                "tools": [{"name": "Agent", "description": routed_tool["description"]}],
            },
        }
        records.insert(records.index(last), snapshot)
        last["parentUuid"] = snapshot["uuid"]
        transcript.write_text("".join(json.dumps(record) + "\n" for record in records))
        with Session(native_binary, tmp_path / "resumed", env, resume=transcript) as session:
            for action in ("off", "on"):
                result = session.send(f"/smart-router {action}")
                assert f"Smart Router is {action}" in result["result"]
                assert agent_tool(session) == (routed_tool if action == "on" else native_tool)
        for label, overrides, expected in (
            ("omni", {"ISAAC_LAUNCH_MODE": "omni"}, routed_tool),
            ("no-marker", {"UCODE_SESSION_ENV_FILE": str(tmp_path / "missing.json")}, native_tool),
        ):
            with Session(native_binary, tmp_path / label, {**env, **overrides}) as session:
                assert agent_tool(session) == expected
    finally:
        shutil.rmtree(controls.parent)


def test_native_process_inherits_settings_and_runs_python_cli(native_binary, tmp_path):
    # The child checks launch settings before replacing itself with the real disabled router.
    script = (
        "import os, sys; "
        "assert os.environ['ENABLE_SMART_ROUTING_V2'] == '0'; "
        "assert os.environ['OAUTH_TOKEN'] == 'fixture-dummy-token'; "
        "os.execv(sys.executable, [sys.executable, '-m', 'ucode.cli', "
        "'claude-router-hook', 'route-subagent-mod', '--host', 'https://example.com'])"
    )
    with Session(
        native_binary,
        tmp_path / "process with spaces",
        {
            "ENABLE_SMART_ROUTING_V2": "0",
            "ENABLE_SMART_ROUTING_SUBAGENT_ONLY": "0",
            "OAUTH_TOKEN": "fixture-dummy-token",
        },
        route_command=[sys.executable, "-c", script],
    ) as session:
        result = session.observe()["route"]
        assert result["exitCode"] == 0, result
        assert result["stdout"] == result["stderr"] == "", result
