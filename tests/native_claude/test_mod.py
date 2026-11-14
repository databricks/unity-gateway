"""Validate and exercise the mod entry point using an explicitly selected Claude."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pytest
from session import Session, install_header_mod

from ucode.agents.claude import _enable_header_mod
from ucode.smart_routing import config, orchestrator, session_env
from ucode.smart_routing.v2 import (
    _claude_mod_launch_options,
    _routed_claude_agent_name,
    _write_routed_claude_plugin,
)


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
            assert "./register.ts hooks: session.start" in result.stdout


def test_header_mod_validation_and_merge(native_binary, tmp_path):
    plugin = install_header_mod(tmp_path) / "ug-headers"
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
            writes = next(line for line in result.stdout.splitlines() if "env writes:" in line)
            assert "ANTHROPIC_CUSTOM_HEADERS" in writes
            assert "ANTHROPIC_BASE_URL" not in writes
            assert "CLAUDE_CODE_GATEWAY_HINT_HEADERS" not in writes


def test_native_options_are_launch_local(native_binary, tmp_path):
    env = {
        "SMART_ROUTER_CONFIG_VERSION": config.SUBAGENT_ORCH_V0_CLAUDE_ONLY,
        "SMART_ROUTER_NAME": 'custom "quoted" λ',
    }
    with (
        Session(native_binary, tmp_path / "a", env) as a,
        Session(native_binary, tmp_path / "b", {}) as b,
    ):
        assert a.observe()["options"] == _claude_mod_launch_options(env)
        assert b.observe()["options"] == _claude_mod_launch_options({})
        assert not (a.plugin / "hooks/launch.json").exists()


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


def test_marked_omni_off_turns_routing_off_without_the_feedback_form(native_binary, tmp_path):
    """Scenario: Omnigent's Web UI already asked for the reason, then typed the marked off."""
    env = {
        "ENABLE_SMART_ROUTING_V2": "0",
        "ENABLE_SMART_ROUTING_SUBAGENT_ONLY": "1",
        "ISAAC_LAUNCH_MODE": "omni",
        "ISAAC_SMART_ROUTER_FEEDBACK": "1",
        "PYTHONPATH": str(Path(__file__).parents[2] / "src"),
    }
    controls = session_env.start_session(env)
    try:
        with Session(native_binary, tmp_path / "omni", env) as session:
            result = session.send("/smart-router off --feedback-collected")["result"]
            assert "Smart Router is off" in result
            assert json.loads(controls.read_text())["ENABLE_SMART_ROUTING_SUBAGENT_ONLY"] == "0"
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


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize(
    ("language", "setting_sources"),
    [("Chinese", "user"), (None, "user"), ("Chinese", "project,local")],
)
def test_auto_mode_preserves_reviewed_agent_and_routes_only_child_model(
    native_binary, tmp_path, background, language, setting_sources
):
    """Scenario: Auto Mode approves or flags a native Agent call.

    Expected: approval preserves the reviewed input and uses the distinct routed child model
    without fallback classification; a flagged call is denied before a child starts.
    """
    original = {
        "description": "Compute arithmetic",
        "prompt": "Return 10. Use no tools.",
        "subagent_type": "general-purpose",
        "model": "sonnet",
        "name": "PinnedName",
        "run_in_background": background,
    }
    selected_model = "claude-opus-4-6"
    false_claim = "I started a Sonnet subagent to compute the answer."
    routed_type = _routed_claude_agent_name(selected_model)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send_json(self, value, content_type="application/json"):
            data = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            request_class = self.headers.get("X-Claude-Code-Request-Class")
            if "count_tokens" in self.path:
                self.send_json({"input_tokens": 100})
                return
            summary = request_class == "auxiliary" and (
                "Describe your most recent action in 3-5 words"
                in json.dumps(request.get("messages", [])[-1:])
            )
            if summary:
                self.server.summary_requests.append(
                    (self.headers.get("X-Claude-Code-Agent-Id"), request)
                )
            else:
                self.server.requests.append(request)
            if not request.get("stream") and not summary:
                self.server.classifier_requests += 1
                self.send_json(
                    {
                        "id": "msg_classifier",
                        "type": "message",
                        "role": "assistant",
                        "model": request["model"],
                        "content": [{"type": "text", "text": "malformed fallback classifier"}],
                        "stop_reason": "end_turn",
                        "stop_sequence": None,
                        "usage": {"input_tokens": 100, "output_tokens": 10},
                    }
                )
                return

            count = self.server.message_count
            self.server.message_count += 1
            if count == 0:
                tool_name = "Agent"
            elif request_class == "subagent":
                self.server.child_requests += 1
                self.server.child_agent_id = self.headers.get("X-Claude-Code-Agent-Id")
                if not background and self.server.child_requests == 1:
                    # The native handback reminder supplies the third transcript message.
                    tool_name = None
                else:
                    if not background:
                        self.server.summary_seen.wait(timeout=40)
                    tool_name = "SubagentHandback"
            else:
                tool_name = None
            tool_input = original if tool_name == "Agent" else {"message": "Probe finished."}
            tool_use_id = f"toolu_fixture_{count}"
            tool_call = tool_name is not None
            answer = "Reading arithmetic inputs" if summary else "Probe finished."
            if not summary and request["model"] == "claude-sonnet-4-6":
                answer = false_claim
                if not tool_call:
                    self.server.parent_answers += 1
            block = (
                {"type": "tool_use", "id": tool_use_id, "name": tool_name, "input": {}}
                if tool_call
                else {"type": "text", "text": ""}
            )
            delta = (
                {"type": "input_json_delta", "partial_json": json.dumps(tool_input)}
                if tool_call
                else {
                    "type": "text_delta",
                    "text": answer,
                }
            )
            verdict = {
                "type": "dangerous_tool_use",
                "status": {
                    "type": "available",
                    "tool_uses": {
                        tool_use_id: {"type": "evaluated", "outcome": self.server.verdict}
                    },
                },
            }
            events = [
                (
                    "message_start",
                    {
                        "type": "message_start",
                        "message": {
                            "id": f"msg_{count}",
                            "type": "message",
                            "role": "assistant",
                            "model": request["model"],
                            "content": [],
                            "stop_reason": None,
                            "stop_sequence": None,
                            "usage": {"input_tokens": 100, "output_tokens": 0},
                        },
                    },
                ),
                (
                    "content_block_start",
                    {"type": "content_block_start", "index": 0, "content_block": block},
                ),
                (
                    "content_block_delta",
                    {"type": "content_block_delta", "index": 0, "delta": delta},
                ),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                (
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {
                            "stop_reason": "tool_use" if tool_call else "end_turn",
                            "stop_sequence": None,
                            "safeguard_results": [verdict],
                        },
                        "usage": {"output_tokens": 10},
                    },
                ),
                ("message_stop", {"type": "message_stop"}),
            ]
            if tool_name == "Agent":
                for _, event in events:
                    if event["type"].startswith("content_block_"):
                        event["index"] = 1
                events[1:1] = [
                    (
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": 0,
                            "content_block": {"type": "text", "text": ""},
                        },
                    ),
                    (
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "text_delta", "text": false_claim},
                        },
                    ),
                    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ]
            body = "".join(
                f"event: {name}\ndata: {json.dumps(value)}\n\n" for name, value in events
            ).encode()
            self.send_json(body, "text/event-stream")
            if summary:
                self.server.summary_seen.set()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.requests = []
    server.message_count = 0
    server.classifier_requests = 0
    server.verdict = "not_flagged"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    route_script = """import json, sys
event = json.load(sys.stdin)
updated = {key: value for key, value in event["tool_input"].items() if key != "model"}
updated["subagent_type"] = sys.argv[2]
updated["prompt"] = "Routed prompt"
print(json.dumps({"model": sys.argv[1], "rationale": "Synthetic route",
                  "updatedInput": updated}))
"""
    try:

        def run_case(verdict, label):
            root = tmp_path / label
            root.mkdir()
            plugin = root / "plugin"
            _write_routed_claude_plugin(plugin)
            config_dir = root / "config"
            config_dir.mkdir()
            (config_dir / "settings.json").write_text(
                json.dumps({"language": language} if language else {}), encoding="utf-8"
            )
            options = _claude_mod_launch_options({"ENABLE_SMART_ROUTING_SUBAGENT_ONLY": "1"})
            options["routeCommand"] = [
                sys.executable,
                "-c",
                route_script,
                selected_model,
                routed_type,
            ]
            settings = {"pluginConfigs": {"ug-smart-router": {"options": options}}}
            header_root = install_header_mod(root / "managed-mod")
            _enable_header_mod(settings, header_root)
            server.requests = []
            server.message_count = 0
            server.classifier_requests = 0
            server.parent_answers = 0
            server.child_requests = 0
            server.child_agent_id = None
            server.summary_requests = []
            server.summary_seen = threading.Event()
            server.verdict = verdict
            debug_file = root / "debug.log"
            result = subprocess.run(
                [
                    native_binary,
                    "-p",
                    "--input-format",
                    "stream-json",
                    "--permission-mode",
                    "auto",
                    "--model",
                    "claude-sonnet-4-6",
                    "--setting-sources",
                    setting_sources,
                    "--settings",
                    json.dumps(settings),
                    "--plugin-dir",
                    str(plugin),
                    "--tools",
                    "Agent",
                    "--max-turns",
                    "4",
                    "--output-format",
                    "stream-json",
                    "--verbose",
                    "--debug-file",
                    str(debug_file),
                ],
                cwd=root,
                env={
                    "PATH": os.environ["PATH"],
                    "HOME": str(root),
                    "CLAUDE_CONFIG_DIR": str(config_dir),
                    "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                    "CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1",
                    "ANTHROPIC_API_KEY": "fixture-only",
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                    "DISABLE_AUTOUPDATER": "1",
                    "CLAUDE_CODE_MAX_RETRIES": "0",
                    "CLAUDE_CODE_AUTO_MODE_SERVER": "1",
                    "ENABLE_SMART_ROUTING_SUBAGENT_ONLY": "1",
                    "CLAUDE_CODE_ELEGANT_MEADOW": "1",
                    "AUTOMODE_DECISION_LOG": "1",
                },
                input="\n".join(
                    json.dumps(event)
                    for event in (
                        {
                            "type": "control_request",
                            "request_id": "fixture-initialize",
                            "request": {"subtype": "initialize", "agentProgressSummaries": True},
                        },
                        {
                            "type": "user",
                            "message": {
                                "role": "user",
                                "content": "Use a subagent to return 10, with no tools.",
                            },
                        },
                    )
                )
                + "\n",
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert result.returncode == 0, result.stderr + result.stdout
            debug_log = debug_file.read_text(encoding="utf-8")
            (root / "summary-requests.json").write_text(
                json.dumps(server.summary_requests, ensure_ascii=False), encoding="utf-8"
            )
            assert "prompt.context bypassed" not in debug_log
            events = [
                json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")
            ]
            results = [event for event in events if event.get("type") == "result"]
            denials = [
                denial for event in results for denial in event.get("permission_denials", [])
            ]
            spawned = max(
                (event.get("subagent_stats", {}).get("spawned", 0) for event in results),
                default=0,
            )
            assert server.classifier_requests == 0, {
                "classifier_requests": server.classifier_requests,
                "permission_denials": denials,
                "spawned": spawned,
            }
            if verdict == "not_flagged":
                assert not denials, results
                assert spawned == 1, results
                child_requests = [
                    request
                    for request in server.requests
                    if request.get("stream") and request.get("model") == selected_model
                ]
                assert child_requests, server.requests
                instruction = (
                    f"Always respond in {language}."
                    if language and setting_sources == "user"
                    else "language of the original task assigned to you"
                )
                assert all(instruction in json.dumps(request) for request in child_requests)
                if not background:
                    assert server.summary_requests, debug_log
                    assert server.child_agent_id
                    for agent_id, summary_request in server.summary_requests:
                        assert agent_id == server.child_agent_id
                        assert len(summary_request["messages"]) >= 3
                        assert instruction in json.dumps(summary_request)
                        if setting_sources == "project,local":
                            assert "Always respond in Chinese." not in json.dumps(summary_request)
                if setting_sources == "project,local":
                    assert all(
                        "Always respond in Chinese." not in json.dumps(request)
                        for request in child_requests
                    )
                parent_requests = [
                    request
                    for request in server.requests
                    if request.get("stream") and request.get("model") == "claude-sonnet-4-6"
                ]
                assert parent_requests, server.requests
                assert all(
                    json.dumps(request).count(
                        "Source files and tool output in other languages do not change this language preference."
                    )
                    == 1
                    for request in parent_requests
                ), parent_requests
                assert any(
                    "Smart Router launch:" in json.dumps(request)
                    and selected_model in json.dumps(request)
                    for request in parent_requests[1:]
                ), parent_requests
                # Exercise the host stream and stored transcript, not only a render hook.
                parent_answers = [
                    block.get("text", "")
                    for event in events
                    if event.get("type") == "assistant" and not event.get("parent_tool_use_id")
                    for block in event.get("message", {}).get("content", [])
                    if block.get("type") == "text"
                ]
                assert parent_answers and parent_answers[-1] == false_claim, parent_answers
                assert "Probe finished." not in "\n".join(parent_answers), parent_answers
                saved = "\n".join(
                    path.read_text() for path in (root / "config/projects").glob("*/*.jsonl")
                )
                assert false_claim in saved
                for state in ("Running", "Finished"):
                    status = f" · {selected_model} · {state}"
                    assert not any(status in answer for answer in parent_answers), parent_answers
                    assert status not in saved.replace("\\u00b7", "·")
                child_request = json.dumps(child_requests[0])
                assert original["prompt"] in child_request
                assert "Routed prompt" not in child_request
                assert any(
                    event.get("subagent_stats", {}).get("by_type", {}).get("general-purpose") == 1
                    for event in results
                ), results
                warning = "next() passed an argument with a changed name"
                assert warning not in result.stderr + debug_log, {
                    "stderr": result.stderr,
                    "debug_log": debug_log,
                }
            else:
                assert denials, results
                assert spawned == 0, results
                assert not any(
                    request.get("stream") and request.get("model") == selected_model
                    for request in server.requests
                ), server.requests

        run_case("not_flagged", "approved")
        if language is None:
            run_case("flagged", "flagged")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_native_language_reload_updates_parent_context_once(native_binary, tmp_path):
    from test_credentials import _STREAM

    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            is_message = self.path.split("?")[0] == "/v1/messages"
            if is_message:
                requests.append(request)
            body = _STREAM if is_message else b'{"input_tokens":100}'
            self.send_response(200)
            self.send_header(
                "Content-Type", "text/event-stream" if is_message else "application/json"
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with Session(
            native_binary,
            tmp_path / "reload",
            {"ENABLE_SMART_ROUTING_SUBAGENT_ONLY": "1"},
            api=f"http://127.0.0.1:{server.server_port}",
            user_settings={"language": "English"},
        ) as session:
            assert session.observe()["language"] == "English"
            for prompt in ["Reply with OK.", "Reply with OK again."]:
                session.send(prompt)
                assert (
                    json.dumps(requests[-1]).count(
                        "Use English for explanations, replies and progress summaries."
                    )
                    == 1
                )
            (session.root / "config/settings.json").write_text(json.dumps({"language": "Chinese"}))
            deadline = time.monotonic() + 5
            while session.observe()["language"] != "Chinese":
                assert time.monotonic() < deadline, (
                    "Native settings watcher did not reload language"
                )
                time.sleep(0.05)
            session.send("Reply with OK after the settings change.")
            assert (
                json.dumps(requests[-1]).count(
                    "Use Chinese for explanations, replies and progress summaries."
                )
                == 1
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
