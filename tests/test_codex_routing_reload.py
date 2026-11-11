from __future__ import annotations

import asyncio
import copy
import json
import os
import signal
import socket
import subprocess
import sys
import time
import tomllib
from contextlib import suppress
from queue import SimpleQueue
from types import SimpleNamespace

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from ucode.constants import SMART_ROUTING_ENV_KEYS
from ucode.smart_routing import codex_interposer, codex_reload, v2
from ucode.smart_routing.codex_reload import RestartContinuation, routing_config

CONFIG = {
    "model_provider": "Databricks",
    "model_providers": {
        "Databricks": {
            "name": "Databricks",
            "base_url": "https://example.databricks.com/responses",
            "http_headers": {"databricks-smart-router-recipe": "custom", "User-Agent": "preserve"},
        }
    },
    "hooks": {"UserPromptSubmit": []},
    "responses_api_metadata": {"other": "preserve"},
}


def test_off_on_restores_launch_recipe_without_mutating_launch_config():
    original = copy.deepcopy(CONFIG)
    for enabled, recipe in [(False, "DISABLED"), (True, "custom")]:
        result = routing_config(CONFIG, enabled)
        assert result["model_providers"]["Databricks"]["http_headers"] == {
            "Databricks-Smart-Router-Recipe": recipe,
            "User-Agent": "preserve",
        }
        assert result["responses_api_metadata"] == {
            "other": "preserve",
            "smart_router_recipe_name": recipe,
        }
        assert result["hooks"] == CONFIG["hooks"]
    assert CONFIG == original


def test_missing_launch_recipe_fails():
    config = copy.deepcopy(CONFIG)
    config["model_providers"]["Databricks"]["http_headers"].clear()
    with pytest.raises(RuntimeError, match="nonempty launch recipe"):
        routing_config(config, True)


def test_session_controls_use_explicit_off_and_restore_launch_baseline(tmp_path):
    controls = tmp_path / "env.json"
    controls.write_text("{}")
    assert codex_reload.session_routing_enabled(controls)
    controls.write_text(json.dumps(dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")))
    assert not codex_reload.session_routing_enabled(controls)
    controls.write_text("{}")
    assert codex_reload.session_routing_enabled(controls)


@pytest.mark.parametrize("contents", [None, b"{", b"\xff", b"[]", b'{"unknown":"0"}'])
def test_unreadable_session_controls_report_failure_instead_of_enabling(tmp_path, contents):
    controls = tmp_path / "env.json"
    if contents is not None:
        controls.write_bytes(contents)
    with pytest.raises((OSError, UnicodeError, ValueError)):
        codex_reload.session_routing_enabled(controls)


@pytest.mark.parametrize("result", [None, [], {"thread": None}, {"thread": []}])
def test_malformed_resume_does_not_consume_restart_notice(result):
    notices = SimpleQueue()
    continuation = RestartContinuation(notices)
    resumed(continuation, 1, {"id": "root"}, method="thread/start")
    notices.put("DISABLED")
    continuation.before_tui({"id": [], "method": "thread/resume"})
    continuation.before_tui({"id": 2, "method": "thread/resume", "params": None})
    assert continuation.after_resume({"id": [], "result": result}) is None
    continuation.before_tui({"id": 3, "method": "thread/resume"})
    assert continuation.after_resume({"id": 3, "result": result}) is None
    assert not notices.empty()
    assert resumed(continuation, 4, {"id": "root"})["params"]["threadId"] == "root"


def resumed(continuation, identity, thread, *, method="thread/resume", error=False):
    continuation.before_tui({"id": identity, "method": method})
    reply = {"id": identity, "result": {"thread": thread}}
    if error:
        reply = {"id": identity, "error": {"message": "resume failed"}}
    return continuation.after_resume(reply)


@pytest.mark.parametrize(
    "child",
    [
        {"id": "child", "parentThreadId": "root"},
        {"id": "child", "source": {"subAgent": {"thread_spawn": {"parent_thread_id": "root"}}}},
        {"id": "child"},
    ],
)
def test_restart_continues_same_root_once_and_never_a_viewed_child(child):
    notices = SimpleQueue()
    continuation = RestartContinuation(notices)
    assert resumed(continuation, 1, {"id": "root"}, method="thread/start") is None
    notices.put("DISABLED")
    assert resumed(continuation, 2, child) is None
    assert continuation.root_id == "root"
    assert not notices.empty()
    request = resumed(continuation, 3, {"id": "root"})
    assert request["method"] == "turn/start"
    assert request["params"]["threadId"] == "root"
    # Native resume owns model, cwd, sandbox, and approvals; the continuation sends only input.
    assert set(request["params"]) == {"threadId", "input"}
    prompt = request["params"]["input"][0]["text"]
    assert "Smart Router is now off" in prompt
    assert "followup_task" in prompt
    assert "Do not spawn replacement agents" in prompt
    assert "started, not that it finished" in prompt
    assert "rerun any command that did not exit successfully" in prompt
    assert continuation.take_response({"id": request["id"], "result": {"turn": {"id": "t"}}})
    assert not continuation.take_response({"id": 3, "result": {}})
    assert resumed(continuation, 4, {"id": "root"}) is None


def test_failed_resume_keeps_recovery_pending_and_rapid_toggles_use_latest_recipe():
    notices = SimpleQueue()
    continuation = RestartContinuation(notices)
    resumed(continuation, 1, {"id": "root"}, method="thread/start")
    notices.put("DISABLED")
    notices.put("custom")
    assert resumed(continuation, 2, {"id": "root"}, error=True) is None
    assert not notices.empty()
    request = resumed(continuation, 3, {"id": "root"})
    assert "Smart Router is now on" in request["params"]["input"][0]["text"]
    assert continuation.recipe == "custom"
    assert notices.empty()


def test_temporary_title_thread_does_not_replace_conversation_root():
    notices = SimpleQueue()
    continuation = RestartContinuation(notices)
    resumed(continuation, 1, {"id": "root"}, method="thread/start")
    continuation.before_tui({"id": 2, "method": "thread/start", "params": {"ephemeral": True}})
    assert continuation.after_resume({"id": 2, "result": {"thread": {"id": "title"}}}) is None
    assert continuation.root_id == "root"
    notices.put("DISABLED")
    request = resumed(continuation, 3, {"id": "root"})
    assert request["params"]["threadId"] == "root"


@pytest.mark.parametrize("continuation_error", [False, True])
def test_interposer_reconnect_preserves_first_prompt_routing_and_hides_internal_reply(
    monkeypatch, continuation_error
):
    async def scenario():
        calls = []
        route_calls = []
        logs = []
        notices = SimpleQueue()
        engine_connections = []
        passthrough = [
            b"binary",
            "{",
            "null",
            "[]",
            '{"id": [], "method": "thread/resume"}',
            '{"id": 0, "method": "thread/resume", "params": null}',
            '{"id": [], "result": null}',
        ]

        def decide(*args, **kwargs):
            route_calls.append(args)
            return codex_interposer.routing.RoutingDecision(
                model="gpt-selected", rationale="small"
            ), None

        monkeypatch.setattr(codex_interposer.codex_routing, "request_routing_decision", decide)

        async def engine(ws):
            engine_connections.append(ws)
            try:
                async for raw in ws:
                    if raw in passthrough:
                        await ws.send(raw)
                        continue
                    message = json.loads(raw)
                    calls.append(message)
                    method = message["method"]
                    result = {}
                    if method in ("thread/start", "thread/resume"):
                        result = {"thread": {"id": "root"}}
                    if method == "turn/start":
                        if str(message["id"]).startswith("ug-restart-") and continuation_error:
                            await ws.send(
                                json.dumps({"id": message["id"], "error": {"message": "busy"}})
                            )
                            continue
                        result = {"turn": {"id": "turn"}}
                    await ws.send(json.dumps({"id": message["id"], "result": result}))
                    if method == "turn/start":
                        await ws.send(
                            json.dumps(
                                {
                                    "method": "turn/started",
                                    "params": {"threadId": "root", "turn": {"id": "turn"}},
                                }
                            )
                        )
            except ConnectionClosed:
                pass

        async with serve(engine, "127.0.0.1", 0) as native:
            upstream = f"ws://127.0.0.1:{native.sockets[0].getsockname()[1]}"
            interposer = await codex_interposer._serve(
                "127.0.0.1",
                0,
                upstream,
                None,
                logs.append,
                workspace="https://example.databricks.com",
                token_provider=lambda: "test-token",
                restart_notices=notices,
            )
            uri = f"ws://127.0.0.1:{interposer.sockets[0].getsockname()[1]}"
            try:
                async with connect(uri) as tui:
                    for raw in passthrough:
                        await tui.send(raw)
                        assert await tui.recv() == raw
                    await tui.send(json.dumps({"id": 1, "method": "thread/start", "params": {}}))
                    assert json.loads(await tui.recv())["id"] == 1
                    await tui.send(
                        json.dumps(
                            {
                                "id": 2,
                                "method": "turn/start",
                                "params": {
                                    "threadId": "root",
                                    "model": "gpt-selected",
                                    "input": [{"type": "text", "text": "first prompt"}],
                                },
                            }
                        )
                    )
                    assert json.loads(await tui.recv())["id"] == 2
                    assert json.loads(await tui.recv())["method"] == "turn/started"
                    notices.put("DISABLED")
                    await engine_connections[-1].close(code=1012)
                    with pytest.raises(ConnectionClosed):
                        await tui.recv()
                async with connect(uri) as tui:
                    await tui.send(
                        json.dumps(
                            {
                                "id": 3,
                                "method": "thread/resume",
                                "params": {"threadId": "root", "sandbox": "workspace-write"},
                            }
                        )
                    )
                    assert json.loads(await tui.recv())["id"] == 3
                    notification = json.loads(await tui.recv())
                    assert "id" not in notification
                    assert notification["method"] == (
                        "error" if continuation_error else "turn/started"
                    )
                    if continuation_error:
                        assert "Send a message" in notification["params"]["error"]["message"]
                    await tui.send(
                        json.dumps(
                            {
                                "id": 4,
                                "method": "turn/start",
                                "params": {
                                    "threadId": "root",
                                    "model": "gpt-user-choice",
                                    "input": [{"type": "text", "text": "next prompt"}],
                                },
                            }
                        )
                    )
                    assert json.loads(await tui.recv())["id"] == 4
                    assert json.loads(await tui.recv())["method"] == "turn/started"
                assert len(route_calls) == 1
                assert calls[-1]["params"]["model"] == "gpt-user-choice"
                resume = next(call for call in calls if call["method"] == "thread/resume")
                assert resume["params"] == {"threadId": "root", "sandbox": "workspace-write"}
                assert sum(str(call["id"]).startswith("ug-restart-") for call in calls) == 1
                assert any("[RESTART FAILED]" in log for log in logs) == continuation_error
            finally:
                interposer.close()
                await interposer.wait_closed()

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


@pytest.mark.parametrize(
    "failure",
    [None, "startup", "restart", "exit", "spawn", "interrupt", "interrupt-timeout", "controls"],
)
def test_launcher_restarts_only_server_and_cleans_up_owned_processes(
    tmp_path, monkeypatch, failure
):
    controls = tmp_path / "env.json"
    controls.write_text("{}")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(v2, "CODEX_APP_SERVER_LOG", tmp_path / "app-server.log")
    monkeypatch.setenv(v2.SESSION_PYTHON_ENV_VAR, "/test/python")
    monkeypatch.setattr(v2, "launch_token", lambda *_: "test-token")
    monkeypatch.setattr(v2, "_prepare_smart_router_session", lambda *_: controls)
    monkeypatch.setattr(v2, "_v2_hooks", lambda *_: CONFIG["hooks"])
    monkeypatch.setattr(v2, "_free_port", lambda: 41001)
    processes = []
    stopped = []
    ready_calls = []
    interposer_options = {}
    waits = 0
    warnings = []
    monkeypatch.setattr(v2, "print_warning", warnings.append)
    read_controls = codex_reload.read_session_environment

    def read(path):
        if failure == "controls" and waits in (2, 3):
            raise PermissionError("temporarily unreadable")
        return read_controls(path)

    monkeypatch.setattr(codex_reload, "read_session_environment", read)

    def ready(*args, **kwargs):
        ready_calls.append(args)
        return failure != "startup" and not (failure == "restart" and len(ready_calls) == 2)

    class Process:
        def __init__(self, argv, **kwargs):
            if failure == "spawn" and len(processes) == 2:
                raise OSError("failed to spawn app-server")
            self.argv = argv
            self.pid = 41000 + len(processes)
            self.env = kwargs.get("env")
            self.starts_session = kwargs.get("start_new_session", False)
            self.terminated = False
            self.killed = False
            self.signals = []
            processes.append(self)

        def poll(self):
            return 1 if failure == "exit" else None

        def wait(self, timeout=None):
            nonlocal waits
            if "--remote" not in self.argv:
                return 0
            if self.terminated:
                return 130
            if self.signals:
                if failure == "interrupt-timeout":
                    raise subprocess.TimeoutExpired(self.argv, timeout)
                return 130
            waits += 1
            if failure in ("interrupt", "interrupt-timeout"):
                raise KeyboardInterrupt
            if waits == 1:
                controls.write_text(json.dumps(dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")))
            elif waits == 2 or failure == "controls" and waits in (3, 4):
                controls.write_text("{}")
            else:
                return 7
            raise subprocess.TimeoutExpired(self.argv, timeout)

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

        def send_signal(self, value):
            self.signals.append(value)

    def start_interposer(*args, **kwargs):
        interposer_options.update(kwargs)
        return 41002, lambda: stopped.append(True)

    def taskkill(argv, **kwargs):
        assert argv[1:] == ["/PID", argv[2], "/T", "/F"]
        process = next(process for process in processes if str(process.pid) == argv[2])
        process.terminate()
        return SimpleNamespace(returncode=0)

    def killpg(pid, signum):
        process = next(process for process in processes if process.pid == pid)
        if signum == signal.SIGTERM:
            process.terminate()

    monkeypatch.setattr(v2, "os", SimpleNamespace(name=os.name, environ=os.environ, killpg=killpg))
    monkeypatch.setattr(v2, "_wait_for_app_server", ready)
    monkeypatch.setattr(v2.subprocess_cross_os, "popen", Process)
    monkeypatch.setattr(v2.subprocess_cross_os, "run", taskkill)
    monkeypatch.setattr(codex_interposer, "start_interposer_thread", start_interposer)
    expected = (
        SystemExit
        if failure in (None, "interrupt", "interrupt-timeout", "controls")
        else OSError
        if failure == "spawn"
        else RuntimeError
    )
    with pytest.raises(expected) as exc:
        v2.launch_codex(
            {"workspace": "https://example.databricks.com", "codex_models": ["gpt-original"]},
            [],
            binary="/test/codex",
            start_model="gpt-original",
            render_overlay=lambda *_a, **_kw: copy.deepcopy(CONFIG),
        )
    servers = [process for process in processes if "app-server" in process.argv]
    assert all(process.terminated for process in servers)
    if failure != "startup":
        assert stopped == [True]
    if failure in ("restart", "exit", "spawn"):
        assert processes[1].terminated
    if failure in ("interrupt", "interrupt-timeout"):
        assert processes[1].signals == [v2.signal.SIGINT]
        assert exc.value.code == 130
        assert processes[1].terminated == (failure == "interrupt-timeout")
    if failure == "controls":
        assert len(warnings) == 1
        assert "Keeping the current routing state" in warnings[0]
    if failure in (None, "controls"):
        assert exc.value.code == 7
        assert len(processes) == 4  # three server processes, one TUI
        assert all(server.argv[-2:] == ["--listen", "ws://127.0.0.1:41001"] for server in servers)
        assert all(server.env["CODEX_HOME"] == str(tmp_path) for server in servers)
        assert all(server.starts_session == (os.name != "nt") for server in servers)
        configs = [tomllib.loads("\n".join(server.argv[3:-2:2])) for server in servers]
        assert [
            config["responses_api_metadata"]["smart_router_recipe_name"] for config in configs
        ] == ["custom", "DISABLED", "custom"]
        assert [
            config["model_providers"]["Databricks"]["http_headers"][
                "Databricks-Smart-Router-Recipe"
            ]
            for config in configs
        ] == ["custom", "DISABLED", "custom"]
        assert all(config["hooks"] == CONFIG["hooks"] for config in configs)
        assert interposer_options["restart_notices"].get_nowait() == "DISABLED"
        assert interposer_options["restart_notices"].get_nowait() == "custom"


def test_shutdown_kills_unresponsive_server_with_bounded_wait():
    calls = []

    class Process:
        def terminate(self):
            calls.append("terminate")

        def wait(self, timeout):
            assert timeout == v2.PROCESS_SHUTDOWN_TIMEOUT_SECONDS
            calls.append("wait")
            if calls.count("wait") == 1:
                raise subprocess.TimeoutExpired("codex", timeout)

        def kill(self):
            calls.append("kill")

    v2._stop_codex_process(Process())
    assert calls == ["terminate", "wait", "kill", "wait"]


@pytest.mark.parametrize("wrapper_ignores_sigterm", [False, True])
def test_forced_shutdown_releases_native_descendants_listening_port(
    tmp_path, monkeypatch, wrapper_ignores_sigterm
):
    ready = tmp_path / "ready"
    child = """
import signal, socket, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
listener = socket.socket()
listener.bind(('127.0.0.1', 0))
listener.listen()
Path(sys.argv[1] + '.tmp').write_text(str(listener.getsockname()[1]))
Path(sys.argv[1] + '.tmp').replace(sys.argv[1])
time.sleep(60)
"""
    wrapper = """
import signal, subprocess, sys
if sys.argv.pop(1) == 'ignore':
    signal.signal(signal.SIGTERM, lambda *_: None)
subprocess.Popen([sys.executable, '-c', *sys.argv[1:]]).wait()
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            wrapper,
            "ignore" if wrapper_ignores_sigterm else "exit",
            child,
            str(ready),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=os.name != "nt",
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists():
            assert process.poll() is None
            assert time.monotonic() < deadline, "native descendant did not bind its port"
            time.sleep(0.01)
        port = int(ready.read_text())
        if os.name != "nt":
            monkeypatch.setattr(v2, "PROCESS_SHUTDOWN_TIMEOUT_SECONDS", 0.1)
        v2._stop_codex_process(process, process_tree=True, port=port)
        with socket.socket() as replacement:
            replacement.bind(("127.0.0.1", port))
    finally:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=5
            )
        else:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


@pytest.mark.parametrize("returncode,wait_times_out", [(0, False), (128, False), (1, True)])
def test_windows_shutdown_stops_native_process_tree(monkeypatch, returncode, wait_times_out):
    calls = []
    waits = []

    kills = []

    def wait(**kwargs):
        waits.append(kwargs)
        if wait_times_out and not kills:
            raise subprocess.TimeoutExpired("codex", kwargs["timeout"])

    process = SimpleNamespace(pid=42, poll=lambda: None, wait=wait, kill=lambda: kills.append(1))
    monkeypatch.setattr(v2, "os", SimpleNamespace(name="nt"))

    def taskkill(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=returncode, stderr="Access denied", stdout="")

    monkeypatch.setattr(v2.subprocess_cross_os, "run", taskkill)
    if wait_times_out:
        with pytest.raises(RuntimeError, match="Could not stop.*Access denied"):
            v2._stop_codex_process(process, process_tree=True)
    else:
        v2._stop_codex_process(process, process_tree=True)
    # A failed taskkill still kills and reaps the wrapper before reporting.
    assert kills == ([1] if wait_times_out else [])
    assert waits == [{"timeout": v2.PROCESS_SHUTDOWN_TIMEOUT_SECONDS}] * (1 + len(kills))
    argv, options = calls[0]
    assert argv[0].endswith("taskkill.exe")
    assert argv[1:] == ["/PID", "42", "/T", "/F"]
    assert options["timeout"] == v2.PROCESS_SHUTDOWN_TIMEOUT_SECONDS
    assert options["capture_output"] and options["text"]


def test_shutdown_fails_if_the_server_port_is_still_listening(monkeypatch):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            monkeypatch.setattr(v2, "PROCESS_SHUTDOWN_TIMEOUT_SECONDS", 0.1)
            with pytest.raises(RuntimeError, match=f"port {port} is still in use"):
                v2._stop_codex_process(process, port=port)
            assert process.poll() is not None
        finally:
            process.kill()
            process.wait(timeout=5)
