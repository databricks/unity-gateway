"""Opt-in native component evidence using direct fixture API responses.

This tests real Claude settings behavior, not production UG launch wiring or a live
Databricks gateway. No forwarding proxy is involved. Supply an exact native binary
and expected version explicitly; absent prerequisites fail this selected suite.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from ucode.agents.claude_settings import merge_extra_body
from ucode.session_settings import create_session_settings, update_session_settings


@pytest.fixture(scope="module")
def native_binary():
    value = os.environ.get("UCODE_TEST_CLAUDE_BINARY", "")
    expected = os.environ.get("UCODE_TEST_CLAUDE_VERSION", "")
    assert value and expected, (
        "Set UCODE_TEST_CLAUDE_BINARY and UCODE_TEST_CLAUDE_VERSION explicitly."
    )
    binary = Path(value)
    assert binary.is_absolute() and binary.is_file(), (
        "Use an absolute native Claude executable path."
    )
    result = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{expected} (Claude Code)", result.stdout
    return str(binary)


@pytest.fixture
def fixture_api():
    captures = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if self.path.split("?")[0].endswith("count_tokens"):
                raw = b'{"input_tokens":1}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            last = body.get("messages", [{}])[-1].get("content", "")
            child = "CHILD_SETTINGS_PROBE" in json.dumps(last)
            source = "child" if child else "parent"
            captures.append(
                {
                    "session": self.path.split("/")[1],
                    "source": source,
                    "value": body.get("session_test_value", "ABSENT"),
                    "caller": body.get("caller_field"),
                }
            )
            tool_result = isinstance(last, list) and any(
                isinstance(item, dict) and item.get("type") == "tool_result" for item in last
            )
            if not child and not tool_result:
                content = {
                    "type": "tool_use",
                    "id": f"tool_probe_{len(captures)}",
                    "name": "Agent",
                    "input": {},
                }
                delta = {
                    "type": "input_json_delta",
                    "partial_json": json.dumps(
                        {
                            "description": "Check child payload",
                            "prompt": "Reply CHILD_SETTINGS_PROBE complete",
                            "subagent_type": "general-purpose",
                        }
                    ),
                }
                stop = "tool_use"
            else:
                content = {"type": "text", "text": ""}
                delta = {
                    "type": "text_delta",
                    "text": "child complete" if child else "parent complete",
                }
                stop = "end_turn"
            message = {
                "id": f"msg_probe_{len(captures)}",
                "type": "message",
                "role": "assistant",
                "model": body["model"],
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 0},
            }
            events = [
                ("message_start", {"type": "message_start", "message": message}),
                (
                    "content_block_start",
                    {"type": "content_block_start", "index": 0, "content_block": content},
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
                        "delta": {"stop_reason": stop, "stop_sequence": None},
                        "usage": {"output_tokens": 2},
                    },
                ),
                ("message_stop", {"type": "message_stop"}),
            ]
            raw = "".join(
                f"event: {name}\ndata: {json.dumps(event)}\n\n" for name, event in events
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", captures
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class NativeSession:
    def __init__(
        self, binary, api_url, name, cwd, *, watched, initial="initial", runtime_only=False
    ):
        settings = (
            {}
            if initial is None
            else {
                "env": {
                    "CLAUDE_CODE_EXTRA_BODY": merge_extra_body(
                        '{"caller_field":"keep"}', {"session_test_value": initial}
                    )
                }
            }
        )
        self.path = (
            Path(tempfile.mkdtemp(prefix="claude-runtime-settings-")) / "settings.json"
            if runtime_only
            else create_session_settings(settings)
        )
        self.config = self.path.parent if watched else self.path.parent / "config"
        self.config.mkdir(exist_ok=True)
        self.name = name
        self.lines = queue.Queue()
        self.errors = []
        env = {
            key: os.environ[key]
            for key in ("PATH", "SYSTEMROOT", "TEMP", "TMP")
            if key in os.environ
        }
        env.update(
            {
                "HOME": str(self.path.parent),
                "CLAUDE_CONFIG_DIR": str(self.config),
                "ANTHROPIC_BASE_URL": f"{api_url}/{name}",
                "ANTHROPIC_API_KEY": "fixture-dummy-key",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "DISABLE_AUTOUPDATER": "1",
                "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "0",
            }
        )
        args = [
            binary,
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--setting-sources",
            "user",
            "--model",
            "claude-sonnet-4-6",
            "--tools",
            "Agent",
            "--allowedTools",
            "Agent",
        ]
        if not watched and not runtime_only:
            args.extend(["--settings", str(self.path)])
        try:
            self.proc = subprocess.Popen(
                args,
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except BaseException:
            shutil.rmtree(self.path.parent)
            raise
        self.stdout_thread = threading.Thread(
            target=lambda: [self.lines.put(line) for line in self.proc.stdout], daemon=True
        )
        self.stderr_thread = threading.Thread(
            target=lambda: self.errors.extend(self.proc.stderr), daemon=True
        )
        self.stdout_thread.start()
        self.stderr_thread.start()

    def control(self, request):
        identifier = str(uuid.uuid4())
        self.proc.stdin.write(
            json.dumps(
                {
                    "type": "control_request",
                    "request_id": identifier,
                    "request": request,
                }
            )
            + "\n"
        )
        self.proc.stdin.flush()
        deadline = time.monotonic() + 15
        while True:
            try:
                event = json.loads(self.lines.get(timeout=max(0, deadline - time.monotonic())))
            except queue.Empty:
                pytest.fail("Native Claude control response timed out")
            if (
                event.get("type") == "control_response"
                and event.get("response", {}).get("request_id") == identifier
            ):
                response = event["response"]
                assert response["subtype"] == "success", response
                return

    def update_in_memory(self, value):
        # Public TS SDK Query.applyFlagSettings emits this same control request.
        # Await its response instead of relying on a file watcher or a sleep.
        self.control(
            {
                "subtype": "apply_flag_settings",
                "settings": {
                    "env": {
                        "CLAUDE_CODE_EXTRA_BODY": merge_extra_body(
                            '{"caller_field":"keep"}', {"session_test_value": value}
                        )
                    }
                },
            }
        )

    def update(self, value):
        update_session_settings(
            self.path,
            {
                "env": {
                    "CLAUDE_CODE_EXTRA_BODY": merge_extra_body(
                        '{"caller_field":"keep"}', {"session_test_value": value}
                    )
                }
            },
        )
        # Watcher timing is part of the evidence; this does not prove immediate reload.
        time.sleep(1)

    def turn(self, captures):
        before = len(captures)
        self.proc.stdin.write(
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": "Use an Agent child to say hello, then confirm completion.",
                    },
                }
            )
            + "\n"
        )
        self.proc.stdin.flush()
        deadline = time.monotonic() + 35
        while True:
            try:
                event = json.loads(self.lines.get(timeout=max(0, deadline - time.monotonic())))
            except queue.Empty:
                pytest.fail(f"Native Claude turn timed out; stderr: {''.join(self.errors)[-1200:]}")
            if event.get("type") == "result":
                assert not event.get("is_error"), event
                rows = [row for row in captures[before:] if row["session"] == self.name]
                assert {row["source"] for row in rows} == {"parent", "child"}, rows
                return rows

    def close(self):
        try:
            self.proc.stdin.close()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
            self.stdout_thread.join(timeout=5)
            self.stderr_thread.join(timeout=5)
            assert self.proc.returncode == 0, "".join(self.errors)[-1200:]
        finally:
            self.proc.stdout.close()
            self.proc.stderr.close()
            shutil.rmtree(self.path.parent)


def assert_payload(rows, value):
    assert all(row["value"] == value and row["caller"] == "keep" for row in rows), rows


def test_watched_settings_update_parent_and_child_and_isolate_two_sessions(
    native_binary, fixture_api, tmp_path
):
    """Both live native sessions use separate fixture config directories; update only one."""
    url, captures = fixture_api
    with ExitStack() as stack:
        first = NativeSession(native_binary, url, "first", tmp_path, watched=True)
        stack.callback(first.close)
        second = NativeSession(
            native_binary, url, "second", tmp_path, watched=True, initial="other"
        )
        stack.callback(second.close)
        assert_payload(first.turn(captures), "initial")
        assert_payload(second.turn(captures), "other")
        for value in ("updated", "initial"):
            first.update(value)
            assert_payload(first.turn(captures), value)
            assert_payload(second.turn(captures), "other")


def test_startup_settings_file_remains_cached_after_update(native_binary, fixture_api, tmp_path):
    """Record the pinned native version's --settings behavior separately from watched user settings."""
    url, captures = fixture_api
    with ExitStack() as stack:
        session = NativeSession(native_binary, url, "startup", tmp_path, watched=False)
        stack.callback(session.close)
        assert_payload(session.turn(captures), "initial")
        session.update("updated")
        assert_payload(session.turn(captures), "initial")


def test_no_extra_body_setting_adds_no_fixture_field(native_binary, fixture_api, tmp_path):
    url, captures = fixture_api
    with ExitStack() as stack:
        session = NativeSession(native_binary, url, "absent", tmp_path, watched=True, initial=None)
        stack.callback(session.close)
        rows = session.turn(captures)
        assert all(row["value"] == "ABSENT" and row["caller"] is None for row in rows), rows


def test_runtime_control_updates_parent_and_child_without_settings_files(
    native_binary, fixture_api, tmp_path
):
    """Acknowledged in-memory updates change subsequent requests and isolate two live sessions."""
    url, captures = fixture_api
    with ExitStack() as stack:
        first = NativeSession(
            native_binary,
            url,
            "runtime-first",
            tmp_path,
            watched=False,
            initial=None,
            runtime_only=True,
        )
        stack.callback(first.close)
        second = NativeSession(
            native_binary,
            url,
            "runtime-second",
            tmp_path,
            watched=False,
            initial=None,
            runtime_only=True,
        )
        stack.callback(second.close)
        for session in (first, second):
            rows = session.turn(captures)
            assert all(row["value"] == "ABSENT" and row["caller"] is None for row in rows), rows
        second.update_in_memory("other-session")
        for value in ("initial", "updated", "initial"):
            first.update_in_memory(value)
            assert_payload(first.turn(captures), value)
            assert_payload(second.turn(captures), "other-session")
            for session in (first, second):
                assert not session.path.exists()
                assert not (session.config / "settings.json").exists()
