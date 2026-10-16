"""Native Codex with UG's actual hooks and a deterministic local router/provider."""

import argparse
import http.server
import json
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

from websockets.sync.client import connect

from ucode.codex_config import codex_config_args
from ucode.os_compatibility import subprocess_cross_os
from ucode.smart_routing import codex_resume, v2
from ucode.smart_routing.session_env import start_session

PARENT = "gpt-5.1"
CHILD = "gpt-5.1-codex-mini"
LEAF = "gpt-5.1-nano"


def rpc(connection, method, params, ident=1):
    connection.send(json.dumps({"id": ident, "method": method, "params": params}))
    deadline = time.monotonic() + 20
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"{method} timed out")
        message = json.loads(connection.recv(timeout=remaining))
        if message.get("id") == ident:
            if "error" in message:
                raise RuntimeError(message)
            return message["result"]


def initialize(connection):
    rpc(
        connection,
        "initialize",
        {
            "clientInfo": {"name": "ug-resume-reproduction", "version": "1"},
            "capabilities": {"experimentalApi": True},
        },
    )
    connection.send(json.dumps({"method": "initialized", "params": {}}))


def model_metadata(model):
    return {
        "slug": model,
        "display_name": model,
        "description": "Local synthetic fixture",
        "default_reasoning_level": "medium",
        "supported_reasoning_levels": [
            {"effort": effort, "description": effort} for effort in ["low", "medium", "high"]
        ],
        "supports_reasoning_summaries": True,
        "support_verbosity": False,
        "shell_type": "shell_command",
        "visibility": "list",
        "supported_in_api": True,
        "priority": 1,
        "experimental_supported_tools": [],
        "truncation_policy": {"mode": "tokens", "limit": 10000},
        "context_window": 128000,
        "multi_agent_version": "v1",
        "model_messages": {"instructions_template": "Execute the local test fixture."},
    }


class Provider(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, restart, nested, no_effort):
        super().__init__(("127.0.0.1", 0), Handler)
        self.root_actions = ["spawn", "wait", "live", "wait", "close"]
        self.root_actions += ["finish"] if restart else ["resume", "resumed", "wait", "finish"]
        self.child_actions = ["spawn", "wait", "finish"] if nested else []
        self.child_id = self.leaf_id = None
        self.stage = "initial"
        self.records = []
        self.routes = []
        self.results = {}
        self.nested = nested
        self.no_effort = no_effort


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        peer = self.server
        if self.path.endswith("routes:select"):
            selected = LEAF if "nested" in body["task"]["prompt"] else CHILD
            peer.routes.append({"request": body, "selected": selected})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "route_selection": [
                            {
                                "route_option": {
                                    "model": selected,
                                    "harness": "codex",
                                }
                            }
                        ]
                    }
                ).encode()
            )
            return
        meta = body.get("client_metadata") or {}
        parent_id = meta.get("x-codex-parent-thread-id")
        child = bool(self.headers.get("x-openai-subagent"))
        thread_id = meta.get("thread_id")
        reasoning = body.get("reasoning") or {}
        peer.records.append(
            {
                "thread_id": thread_id,
                "parent_id": parent_id,
                "child": child,
                "stage": peer.stage,
                "model": body.get("model"),
                "effort": reasoning.get("effort"),
            }
        )
        for item in body.get("input", []):
            if item.get("type") != "function_call_output":
                continue
            call_id, result = item.get("call_id"), item.get("output")
            peer.results[call_id] = result
            if call_id in ("root-spawn", "child-spawn"):
                try:
                    value = json.loads(result)
                except (ValueError, TypeError):
                    # Preserve native tool failures and complete the parent turn
                    # so the final assertions can report the failed fixture.
                    peer.root_actions = ["finish"]
                    peer.child_actions = ["finish"]
                    continue
                if call_id == "root-spawn":
                    peer.child_id = value["agent_id"]
                else:
                    peer.leaf_id = value["agent_id"]
        is_leaf = child and body["model"] == LEAF
        # After a defective restore, model is no longer a reliable role marker.
        is_leaf = is_leaf or thread_id == peer.leaf_id
        actions = [] if is_leaf else peer.child_actions if child else peer.root_actions
        action = actions.pop(0) if actions else "finish"
        response_id = "response-" + str(len(peer.records))
        if action == "finish":
            item = {
                "type": "message",
                "role": "assistant",
                "id": "msg-" + response_id,
                "content": [{"type": "output_text", "text": "Done."}],
            }
        else:
            target = peer.leaf_id if child else peer.child_id
            if action == "spawn":
                name = "spawn_agent"
                arguments = {
                    "message": "Inspect the nested fixture." if child else "Inspect the fixture.",
                    "model": PARENT,
                }
                if not peer.no_effort:
                    arguments["reasoning_effort"] = "medium" if child else "high"
            elif action == "wait":
                name, arguments = "wait_agent", {"targets": [target], "timeout_ms": 10000}
            elif action in ("live", "resumed"):
                if not child:
                    peer.stage = action
                    if action == "resumed" and peer.nested:
                        peer.child_actions = ["resumed", "wait", "finish"]
                name, arguments = (
                    "send_input",
                    {"target": target, "message": "Continue the fixture."},
                )
            elif action == "close":
                name, arguments = "close_agent", {"target": target}
            elif action == "resume":
                name, arguments = "resume_agent", {"id": target}
            else:
                raise AssertionError(action)
            call_id = ("child-" if child else "root-") + (
                action if action != "wait" else response_id
            )
            item = {
                "type": "function_call",
                "namespace": "multi_agent_v1",
                "name": name,
                "call_id": call_id,
                "arguments": json.dumps(arguments),
            }
        events = [
            {"type": "response.created", "response": {"id": response_id}},
            {"type": "response.output_item.done", "item": item},
            {
                "type": "response.completed",
                "response": {
                    "id": response_id,
                    "usage": {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "total_tokens": 0,
                    },
                },
            },
        ]
        payload = "".join(
            "event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n" for e in events
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def run(args):
    binary = Path(args.binary)
    if not binary.is_absolute() or not binary.is_file():
        raise ValueError("--binary must name an installed absolute Codex executable")
    version = subprocess_cross_os.run(
        [str(binary), "--version"], capture_output=True, text=True, timeout=10, check=True
    )
    if version.stdout.strip() != "codex-cli " + args.version:
        raise ValueError(f"Unexpected Codex version: {version.stdout.strip()}")
    sandbox_binary = shutil.which("bwrap")
    if sandbox_binary is None:
        raise RuntimeError("This reproduction requires Linux with bubblewrap installed")
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    peer = Provider(args.restart, args.nested, args.no_effort)
    fixture_thread = threading.Thread(target=peer.serve_forever, daemon=True)
    fixture_thread.start()
    workspace = f"http://127.0.0.1:{peer.server_port}"
    models = [model_metadata(model) for model in (PARENT, CHILD, LEAF)]
    if args.no_effort:
        for model in models[1:]:
            model.update(
                default_reasoning_level=None,
                supported_reasoning_levels=[],
                supports_reasoning_summaries=False,
            )
    catalog = out / "models.json"
    catalog.write_text(json.dumps({"models": models}))
    runtime, ug_state = out / "codex-home", out / "ug-state"
    runtime.mkdir()
    ug_state.mkdir()
    native_events = []
    root_id = None
    try:
        for phase in range(2 if args.restart else 1):
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            url = f"ws://127.0.0.1:{port}"
            env = {
                key: os.environ[key]
                for key in ("PATH", "HOME", "CODEX_HOME", "TMPDIR")
                if key in os.environ
            }
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
            env["ENABLE_SMART_ROUTING_SUBAGENT_ONLY"] = "1"
            env["ENABLE_SMART_ROUTING_V2"] = "0"
            env["ENABLE_SMART_ROUTER_ORCHESTRATOR"] = "0"
            env.pop("SMART_ROUTER_CONFIG_VERSION", None)
            env["DATABRICKS_BEARER"] = "local-fixture"
            session = start_session(env)
            codex_resume.configure_session(session, url)
            overlay = {
                "model": PARENT,
                "model_reasoning_effort": "low",
                "model_catalog_json": str(catalog),
                "model_provider": "resume_fixture",
                "model_providers.resume_fixture": {
                    "name": "Local fixture",
                    "base_url": workspace + "/v1",
                    "wire_api": "responses",
                    "requires_openai_auth": False,
                    "supports_websockets": False,
                },
                "cli_auth_credentials_store": "ephemeral",
                "chatgpt_base_url": workspace + "/backend-api/",
                "features.multi_agent": True,
                "features.multi_agent_v2": False,
                "features.shell_tool": False,
                "features.hooks": True,
                "agents.max_depth": 3,
                "check_for_update_on_startup": False,
                "web_search": "disabled",
                "history.persistence": "none",
                "approval_policy": "never",
                "sqlite_home": str(out / "state"),
                "hooks": v2._v2_hooks({"workspace": workspace}, [PARENT, CHILD, LEAF]),
            }
            if args.baseline:
                # Keep the real router; omit only the new lifecycle guard hooks.
                overlay["hooks"] = {
                    "PreToolUse": [
                        group
                        for group in overlay["hooks"]["PreToolUse"]
                        if any(
                            "codex-router-hook route-subagent" in hook["command"]
                            for hook in group["hooks"]
                        )
                    ]
                }
            for key in ("UCODE_SESSION_ENV_FILE", "UCODE_SMART_ROUTER_PYTHON"):
                overlay["shell_environment_policy.set." + key] = env[key]
            home_path = os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
            cmd = [
                sandbox_binary,
                "--bind",
                "/",
                "/",
                "--bind",
                str(runtime),
                home_path,
                "--bind",
                str(ug_state),
                str(Path.home() / ".ucode"),
                "--die-with-parent",
                "--",
                args.binary,
                "app-server",
                "--listen",
                url,
                *codex_config_args(overlay),
            ]
            with (out / f"phase-{phase}.stderr.txt").open("w") as stderr:
                process = subprocess_cross_os.popen(
                    cmd,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr,
                    start_new_session=True,
                )
                try:
                    connection = None
                    for _ in range(100):
                        if process.poll() is not None:
                            raise RuntimeError((out / f"phase-{phase}.stderr.txt").read_text())
                        try:
                            connection = connect(url, open_timeout=1)
                            break
                        except OSError:
                            time.sleep(0.05)
                    if connection is None:
                        raise RuntimeError("app-server did not start")
                    with connection:
                        initialize(connection)
                        hooks = rpc(connection, "hooks/list", {"cwds": [str(out)]}, ident=10)
                        (out / f"phase-{phase}.hooks.json").write_text(json.dumps(hooks, indent=2))
                        fixture_hooks = [hook for entry in hooks["data"] for hook in entry["hooks"]]
                        if any(hook["source"] != "sessionFlags" for hook in fixture_hooks):
                            raise RuntimeError("The isolated fixture discovered non-fixture hooks")
                        # Review/approve only the generated test hooks via Codex's native trust API.
                        trust = {
                            h["key"]: {"trusted_hash": h["currentHash"]} for h in fixture_hooks
                        }
                        rpc(
                            connection,
                            "config/batchWrite",
                            {
                                "edits": [
                                    {
                                        "keyPath": "hooks.state",
                                        "value": trust,
                                        "mergeStrategy": "upsert",
                                    }
                                ],
                                "reloadUserConfig": True,
                            },
                            ident=11,
                        )
                        if root_id is None:
                            result = rpc(
                                connection,
                                "thread/start",
                                {
                                    "cwd": str(out),
                                    "model": PARENT,
                                    "modelProvider": "resume_fixture",
                                },
                                ident=12,
                            )
                            root_id = result["thread"]["id"]
                        else:
                            peer.root_actions = ["resume", "resumed", "wait", "finish"]
                            rpc(
                                connection,
                                "thread/resume",
                                {"threadId": root_id, "modelProvider": "resume_fixture"},
                                ident=12,
                            )
                        rpc(
                            connection,
                            "turn/start",
                            {
                                "threadId": root_id,
                                "input": [
                                    {"type": "text", "text": "Execute the synthetic fixture."},
                                ],
                            },
                            ident=13,
                        )
                        deadline = time.monotonic() + 90
                        while True:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise TimeoutError("native fixture exceeded 90 seconds")
                            event = json.loads(connection.recv(timeout=min(45, remaining)))
                            native_events.append(event)
                            if (
                                event.get("method") == "turn/completed"
                                and event["params"]["threadId"] == root_id
                            ):
                                assert event["params"]["turn"]["status"] == "completed", event
                                break
                        if phase == (1 if args.restart else 0):
                            snapshots = [
                                rpc(connection, "thread/read", {"threadId": tid}, ident=30 + i)
                                for i, tid in enumerate(
                                    [peer.child_id] + ([peer.leaf_id] if args.nested else [])
                                )
                            ]
                            (out / "native-settings.json").write_text(
                                json.dumps(snapshots, indent=2)
                            )
                            for snapshot in snapshots:
                                thread = snapshot["thread"]
                                expected = LEAF if thread["id"] == peer.leaf_id else CHILD
                                effort = (
                                    None
                                    if args.no_effort
                                    else "medium"
                                    if expected == LEAF
                                    else "high"
                                )
                                if args.baseline:
                                    expected, effort = PARENT, "low"
                                assert (thread["model"], thread["reasoningEffort"]) == (
                                    expected,
                                    effort,
                                ), thread
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                    (session.parent / "codex-app-server.json").unlink(missing_ok=True)
                    session.unlink()
                    session.parent.rmdir()
    finally:
        peer.shutdown()
        fixture_thread.join(timeout=5)
        assert not fixture_thread.is_alive()
        peer.server_close()
        for name, data in [
            ("requests", peer.records),
            ("routes", peer.routes),
            ("tool-results", peer.results),
            ("events", native_events),
        ]:
            (out / (name + ".json")).write_text(json.dumps(data, indent=2))
    failures = [
        e
        for e in native_events
        if e.get("method") == "hook/completed" and e["params"]["run"]["status"] != "completed"
    ]
    assert not failures, failures
    assert peer.routes, "actual router hook did not run"
    for record in peer.records:
        expected_model = (
            LEAF if record["thread_id"] == peer.leaf_id else CHILD if record["child"] else PARENT
        )
        expected_effort = (
            (None if args.no_effort else "medium" if expected_model == LEAF else "high")
            if record["child"]
            else "low"
        )
        if args.baseline and record["child"] and record["stage"] == "resumed":
            expected_model, expected_effort = PARENT, "low"
        assert (record["model"], record["effort"]) == (expected_model, expected_effort), record
    assert {r["stage"] for r in peer.records if r["thread_id"] == peer.child_id} == {
        "initial",
        "live",
        "resumed",
    }
    if args.nested:
        assert {r["stage"] for r in peer.records if r["thread_id"] == peer.leaf_id} == {
            "initial",
            "resumed",
        }
    result = {
        "passed": True,
        "baseline": args.baseline,
        "restart": args.restart,
        "nested": args.nested,
        "no_effort": args.no_effort,
        "routing_calls": len(peer.routes),
        "children": [r for r in peer.records if r["child"]],
    }
    (out / "result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--version", default="0.159.3")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--restart", action="store_true")
    parser.add_argument("--nested", action="store_true")
    parser.add_argument("--no-effort", action="store_true")
    run(parser.parse_args())
