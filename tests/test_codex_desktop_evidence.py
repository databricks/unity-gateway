"""Reject false positives in the native desktop skill/MCP evidence checks."""

import copy
import io
import json
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from tests.integration.utils import codex_desktop, windows_app
from tests.integration.utils.codex_desktop import (
    MCP_SERVER,
    SKILL_NAME,
    assert_skill_mcp_turn,
    write_search_skill,
)

QUERY = "Databricks Unity Catalog documentation a13b42"
MARKER = "b39c21"
MODEL = "system.ai.gpt-5-6-sol"


def test_persistent_windows_uia_protocol_preserves_process_and_errors():
    helper = (
        "import json,sys\n"
        "print(json.dumps({'event':'ready','metrics':{'init_ms':0}}),flush=True)\n"
        "for line in sys.stdin:\n"
        " r=json.loads(line)\n"
        " print(json.dumps({'ok':r['command'] != 'bad', 'output':str(len(r['args'])), "
        "'error':'bad command'}),flush=True)\n"
    )
    client = windows_app.WindowsUIAProcess([sys.executable, "-u", "-c", helper])
    try:
        pid = client.proc.pid
        assert client.request("tree") == "0"
        assert client.request("type", "private input", "enter") == "2"
        assert client.proc.pid == pid
        with pytest.raises(AssertionError, match="bad command"):
            client.request("bad")
    finally:
        client.close()
    assert client.proc.poll() is not None


def test_persistent_uia_reports_closed_input_and_preserves_stderr(monkeypatch):
    helper = "import sys; print('helper evidence', file=sys.stderr, flush=True)"
    client = windows_app.WindowsUIAProcess([sys.executable, "-u", "-c", helper])
    try:
        client.proc.wait(timeout=5)
        client.ready = True
        # Model an exit immediately after a successful liveness check.
        monkeypatch.setattr(client.proc, "poll", lambda: None)
        with pytest.raises(AssertionError, match="helper input closed"):
            client.request("tree")
        assert client.broken
    finally:
        client.close()
    assert "helper evidence" in client.stderr_output


def test_persistent_uia_preserves_helper_stderr_after_exit():
    helper = "import sys; print('startup failed', file=sys.stderr)"
    client = windows_app.WindowsUIAProcess([sys.executable, "-u", "-c", helper])
    try:
        client.proc.wait(timeout=5)
    finally:
        client.close()
    assert "startup failed" in client.stderr_output


def test_persistent_uia_waits_for_helper_ready_before_sending_command():
    class Responses:
        def __init__(self):
            self.messages = iter(
                [
                    '{"event":"ready","metrics":{"init_ms":12}}',
                    '{"ok":true,"output":"1","error":"","metrics":{"wait_ms":3}}',
                ]
            )
            self.timeouts = []

        def get(self, timeout):
            self.timeouts.append(timeout)
            return next(self.messages)

    client = object.__new__(windows_app.WindowsUIAProcess)
    client.proc = SimpleNamespace(poll=lambda: None, stdin=io.StringIO())
    client.responses = Responses()
    client.broken = False
    client.ready = False
    client.started = windows_app.time.monotonic()
    assert client.request("wait", "ControlType.Text", "Ready", "30") == "1"
    assert client.responses.timeouts == [60, 35]
    assert client.last_metrics["init_ms"] == 12
    assert client.last_metrics["wait_ms"] == 3


def test_persistent_uia_timeout_cannot_reuse_a_stale_response():
    class Responses:
        def get(self, timeout):
            raise windows_app.queue.Empty

    client = object.__new__(windows_app.WindowsUIAProcess)
    client.proc = SimpleNamespace(poll=lambda: None, stdin=io.StringIO())
    client.responses = Responses()
    client.stderr = io.StringIO("uia phase=find-descendants\n")
    client.broken = False
    client.ready = True
    client.init_metrics = {}
    with pytest.raises(AssertionError, match="did not respond") as error:
        client.request("tree")
    assert isinstance(error.value, subprocess.TimeoutExpired)
    assert error.value.stderr == "uia phase=find-descendants\n"
    with pytest.raises(AssertionError, match="unavailable"):
        client.request("tree")


def test_persistent_uia_wait_keeps_the_original_timeout():
    class Responses:
        timeout = 0

        def get(self, timeout):
            self.timeout = timeout
            return '{"ok":true,"output":"1","error":""}'

    client = object.__new__(windows_app.WindowsUIAProcess)
    client.proc = SimpleNamespace(poll=lambda: None, stdin=io.StringIO())
    client.responses = Responses()
    client.broken = False
    client.ready = True
    client.init_metrics = {}
    assert client.request("wait", "ControlType.Text", "answer", "240") == "1"
    assert client.responses.timeout >= 240


def test_windows_click_uses_single_native_condition_wait():
    app = object.__new__(windows_app.WindowsApp)
    actions = []
    app.uia = lambda *args: actions.append(args)
    app.click("ControlType.Button", "Continue", timeout=17)
    assert actions == [("click", "ControlType.Button", "Continue", "17")]


def test_windows_failed_native_click_records_accessibility_tree():
    app = object.__new__(windows_app.WindowsApp)
    app.name = "chatgpt"
    evidence = {}
    app.session = SimpleNamespace(record=lambda name, value: evidence.update({name: value}))
    app.read_tree = lambda: [["ControlType.Text", "stuck", ""]]

    def fail(*args):
        raise AssertionError("target unavailable")

    app.uia = fail
    with pytest.raises(AssertionError, match="target unavailable"):
        app.click("ControlType.Button", "Continue")
    assert evidence["chatgpt-tree.json"]["tree"] == [["ControlType.Text", "stuck", ""]]


def test_windows_uia_uses_persistent_client_for_each_action():
    helper = (
        "import json,sys\n"
        "print(json.dumps({'event':'ready','metrics':{'init_ms':0}}),flush=True)\n"
        "for line in sys.stdin:\n"
        " r=json.loads(line)\n"
        " print(json.dumps({'ok':True, 'output':r['command'], 'error':''}),flush=True)\n"
    )
    app = object.__new__(windows_app.WindowsApp)
    app.proc = SimpleNamespace(poll=lambda: None, pid=123)
    app.name = "chatgpt"
    app.timings = []
    app.uia_process = windows_app.WindowsUIAProcess([sys.executable, "-u", "-c", helper])
    try:
        assert app.uia("tree") == "tree"
        assert app.uia("type", "hello", "enter") == "type"
    finally:
        app.uia_process.close()
    assert [row["action"] for row in app.timings] == ["tree", "type"]


def test_windows_uia_records_native_click_poll_count():
    helper = (
        "import json,sys\n"
        "print(json.dumps({'event':'ready','metrics':{'init_ms':0}}),flush=True)\n"
        "for line in sys.stdin:\n"
        " print(json.dumps({'ok':True, 'output':'3', 'error':'', "
        "'metrics':{'init_ms':11, 'wait_ms':22, 'click_ms':3, 'secret':'private'}}),flush=True)\n"
    )
    app = object.__new__(windows_app.WindowsApp)
    app.proc = SimpleNamespace(poll=lambda: None, pid=123)
    app.name = "chatgpt"
    app.timings = []
    app.uia_process = windows_app.WindowsUIAProcess([sys.executable, "-u", "-c", helper])
    try:
        app.click("ControlType.Button", "Continue")
    finally:
        app.uia_process.close()
    assert app.timings[0]["polls"] == 3
    assert app.timings[0]["init_ms"] == 11
    assert app.timings[0]["wait_ms"] == 22
    assert app.timings[0]["click_ms"] == 3
    start = datetime.fromisoformat(app.timings[0]["started_at"])
    end = datetime.fromisoformat(app.timings[0]["ended_at"])
    assert start.utcoffset().total_seconds() == end.utcoffset().total_seconds() == 0
    assert start <= end
    assert "Continue" not in repr(app.timings) and "private" not in repr(app.timings)


def test_restart_experiment_requires_post_setup_start_connected_and_model_list():
    app = object.__new__(windows_app.WindowsApp)
    app.setup_condition = threading.Condition()
    app.setup_events = [
        {"event": "start_process"},
        {"event": "connected"},
        {"event": "model/list"},
        {"event": "setupStart", "error_code": "null"},
        {"event": "start_process"},
        {"event": "connected"},
        {"event": "model/list"},
    ]
    app.timings = []
    app.wait_for_setup_restart(3, time.monotonic() + 1)
    assert app.timings[-1]["phase"] == "restart-experiment-gate-open"


def test_restart_experiment_rejects_stale_or_out_of_order_responses():
    app = object.__new__(windows_app.WindowsApp)
    app.setup_condition = threading.Condition()
    app.setup_events = [
        {"event": "setupStart", "error_code": "null"},
        {"event": "connected"},
        {"event": "model/list"},
        {"event": "start_process"},
        {"event": "connected"},
        {"event": "stop_process"},
        {"event": "model/list"},
    ]
    app.timings = []
    with pytest.raises(AssertionError, match="post-setup restart"):
        app.wait_for_setup_restart(0, time.monotonic())


def test_restart_experiment_reports_identity_errors_relative_to_first_interaction():
    timings = [
        {"phase": "restart-experiment-gate-open", "started_at": "2026-11-25T00:00:01+00:00"},
        {"phase": "first-interaction-start", "started_at": "2026-11-25T00:00:02+00:00"},
    ]
    events = [
        {"event": "identity-not-ready", "observed_at": "2026-11-25T00:00:00+00:00"},
        {"event": "identity-not-ready", "observed_at": "2026-11-25T00:00:02.250+00:00"},
    ]
    result = windows_app._restart_experiment_summary(timings, events)
    assert result["identity_not_ready_after_gate"] == [
        {"observed_at": "2026-11-25T00:00:02.250+00:00", "relative_to_first_interaction_ms": 250}
    ]
    assert result["gate_opened"] is True


def test_windows_setup_banner_wait_has_a_utc_boundary():
    app = object.__new__(windows_app.WindowsApp)
    app.timings = []
    app.mark_setup_phase("banner-wait-start")
    entry = app.timings[0]
    assert entry["action"] == "setup-phase" and entry["phase"] == "banner-wait-start"
    assert datetime.fromisoformat(entry["started_at"]).utcoffset().total_seconds() == 0


def test_windows_picker_trace_is_forwarded_to_the_helper():
    helper = (
        "import json,sys\n"
        "print(json.dumps({'event':'ready','metrics':{}}),flush=True)\n"
        "for line in sys.stdin:\n"
        " request=json.loads(line)\n"
        " print(json.dumps({'ok':True,'output':'1','error':'','metrics':{},"
        "'diagnostics':{'picker':request['args'][-1]}}),flush=True)\n"
    )
    app = object.__new__(windows_app.WindowsApp)
    app.proc = SimpleNamespace(poll=lambda: None, pid=123)
    app.name = "chatgpt"
    app.timings = []
    app.uia_process = windows_app.WindowsUIAProcess([sys.executable, "-u", "-c", helper])
    try:
        app.click("ControlType.Button", "picker", trace_picker=True)
    finally:
        app.uia_process.close()
    assert app.timings[0]["diagnostics"] == {"picker": "picker"}


def test_windows_uia_samples_legacy_tree_only_once(monkeypatch):
    monkeypatch.setenv("UG_UIA_BASELINE_SAMPLE", "1")
    monkeypatch.setattr(windows_app.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    calls = []
    monkeypatch.setattr(
        windows_app.subprocess,
        "run",
        lambda *args, **kwargs: calls.append(args) or SimpleNamespace(returncode=0),
    )
    app = object.__new__(windows_app.WindowsApp)
    app.proc = SimpleNamespace(pid=123)
    app.powershell = "powershell"
    app.uia = lambda command: "ControlType.Text\tReady\t"
    app.baseline_sampled = False
    app.timings = []
    assert app.read_tree() == [["ControlType.Text", "Ready", ""]]
    assert app.read_tree() == [["ControlType.Text", "Ready", ""]]
    assert len(calls) == 1
    assert app.timings[0]["action"] == "legacy_tree_sample"
    assert app.timings[0]["returncode"] == 0


def test_windows_sandbox_diagnostic_records_only_selection_and_availability(tmp_path):
    directory = tmp_path / ".codex" / ".sandbox"
    directory.mkdir(parents=True)
    log = directory / "sandbox.2026-11-25.log"
    selected = "[2026-11-25 12:34:56.789 codex.exe] Windows sandbox selection: unelevated"
    available = "[2026-11-25 12:34:56.790 codex.exe] MXC availability: false"
    log.write_text(
        "\n".join(
            [
                selected,
                "[2026-11-25 12:34:56.789 codex.exe] token=secret",
                selected + " token=secret",
                available,
            ]
        ),
        encoding="utf-8",
    )
    evidence = {}
    session = SimpleNamespace(
        home=tmp_path, record=lambda name, value: evidence.update({name: value})
    )
    windows_app.record_windows_sandbox_selection(session)
    assert evidence["windows-sandbox-selection.json"] == {"lines": [selected, available]}
    assert "secret" not in repr(evidence)


def test_windows_provisioning_snapshot_excludes_credentials(tmp_path, monkeypatch):
    home = tmp_path / "home"
    codex = home / ".codex"
    (codex / ".sandbox").mkdir(parents=True)
    (codex / ".sandbox-secrets").mkdir()
    (codex / "config.toml").write_text(
        '[windows]\nsandbox = "unelevated"\n[features]\nexperimental_windows_sandbox = true\n'
        'elevated_windows_sandbox = false\n[other]\ntoken = "private-token"\n'
    )
    (codex / ".sandbox/setup_marker.json").write_text(
        json.dumps({"version": 5, "offline_username": "private-name"})
    )
    (codex / ".sandbox-secrets/sandbox_users.json").write_text(
        json.dumps({"version": 5, "offline": {"password": "private-password"}})
    )
    monkeypatch.setattr(
        windows_app,
        "_windows_machine_provisioning",
        lambda home: {"offline": {"exists": True, "disabled": False}},
        raising=False,
    )
    snapshot = windows_app.windows_sandbox_provisioning_snapshot(home)
    assert snapshot["config"]["mode"] == "unelevated"
    assert snapshot["config"]["features"] == {
        "experimental_windows_sandbox": True,
        "elevated_windows_sandbox": False,
    }
    assert snapshot["marker"]["version"] == snapshot["users_file"]["version"] == 5
    assert snapshot["machine"]["offline"]["exists"] is True
    assert datetime.fromisoformat(snapshot["observed_at"]).utcoffset().total_seconds() == 0
    assert "private" not in repr(snapshot)


def test_windows_registered_receipt_reports_only_provisioning_booleans(tmp_path):
    receipt = {
        "user_sid": "private-sid",
        "codex_home": str(tmp_path / ".codex"),
        "runtime": {
            "ready_package": "private-package",
            "retiring": None,
            "accounts": [
                {
                    "account": "offline",
                    "alias_path": "private-path",
                    "cleanup_logon_pending": False,
                },
                {"account": "online", "alias_path": "private-path", "cleanup_logon_pending": True},
            ],
        },
    }
    result = windows_app._sandbox_receipt_state(json.dumps(receipt), tmp_path)
    assert result == {
        "exists": True,
        "home_matches": True,
        "ready_package_present": True,
        "retiring": False,
        "accounts": {
            "offline": {"alias_present": True, "cleanup_pending": False},
            "online": {"alias_present": True, "cleanup_pending": True},
        },
    }
    assert "private" not in repr(result)


def test_windows_machine_provisioning_reports_platform_state(tmp_path):
    state = windows_app._windows_machine_provisioning(tmp_path)
    if sys.platform == "win32":
        assert "accounts" in state and "registration" in state
    else:
        assert state == {"status": "unsupported"}


def test_windows_sandbox_account_state_reports_missing_account():
    api = SimpleNamespace(NetUserGetInfo=lambda server, account, level, buffer: 2221)
    assert windows_app._windows_sandbox_account(api, "CodexSandboxOffline") == {"exists": False}


def test_windows_sandbox_account_reports_disabled_without_password():
    import ctypes
    from ctypes import wintypes

    class UserInfo(ctypes.Structure):
        _fields_ = [
            ("name", wintypes.LPWSTR),
            ("password", wintypes.LPWSTR),
            ("password_age", wintypes.DWORD),
            ("privilege", wintypes.DWORD),
            ("home_dir", wintypes.LPWSTR),
            ("comment", wintypes.LPWSTR),
            ("flags", wintypes.DWORD),
            ("script_path", wintypes.LPWSTR),
        ]

    info = UserInfo()
    info.password = "private-password"
    info.flags = 0x0002
    freed = []

    def get_info(server, account, level, output):
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(info)
        return 0

    api = SimpleNamespace(
        NetUserGetInfo=get_info, NetApiBufferFree=lambda ptr: freed.append(ptr.value)
    )
    result = windows_app._windows_sandbox_account(api, "CodexSandboxOffline")
    assert result == {"exists": True, "disabled": True}
    assert freed == [ctypes.addressof(info)]
    assert "private" not in repr(result)


def test_windows_sandbox_account_empty_success_is_not_reported_as_ready():
    api = SimpleNamespace(NetUserGetInfo=lambda server, account, level, buffer: 0)
    assert windows_app._windows_sandbox_account(api, "CodexSandboxOffline") == {
        "exists": None,
        "error": "empty-response",
    }


def test_windows_sandbox_account_query_error_is_not_reported_as_absent():
    api = SimpleNamespace(NetUserGetInfo=lambda server, account, level, buffer: 5)
    assert windows_app._windows_sandbox_account(api, "CodexSandboxOffline") == {
        "exists": None,
        "error_code": 5,
    }


def test_windows_proxy_diagnostics_allowlist_traceback_without_credentials(tmp_path):
    log = tmp_path / ".ucode/mcp-proxy/private-key/backend.log"
    log.parent.mkdir(parents=True)
    log.write_text(
        "Traceback (most recent call last):\n"
        '  File "C:/Users/private/site-packages/httpx2/_transports/default.py", line 115, in map_httpcore_exceptions\n'
        '  File "C:/Users/private/site-packages/ucode/mcp_proxy.py", line 27, in run\n'
        "httpx2.ConnectTimeout: private-token\n"
        "ug mcp-proxy: upstream returned HTTP 429 private-token\n"
        "ug mcp-proxy: backend exited with code 2 private-token\n"
        "Authorization: Bearer private-token\n",
        encoding="utf-8",
    )
    evidence = {}
    session = SimpleNamespace(
        home=tmp_path, record=lambda name, value: evidence.update({name: value})
    )
    windows_app.record_windows_proxy_diagnostics(session)
    assert evidence["windows-mcp-proxy-diagnostics.json"] == {
        "log_count": 1,
        "events": [
            {"kind": "traceback"},
            {"kind": "frame", "package": "httpx2", "file": "default.py", "line": 115},
            {"kind": "frame", "package": "ucode", "file": "mcp_proxy.py", "line": 27},
            {"kind": "exception", "type": "httpx2.ConnectTimeout"},
            {"kind": "http_status", "status": 429},
            {"kind": "exit_code", "code": 2},
        ],
    }
    assert "private" not in repr(evidence)


def test_windows_app_records_provisioning_before_launch(tmp_path, monkeypatch):
    evidence = {}
    app = object.__new__(windows_app.WindowsApp)
    app.session = SimpleNamespace(
        home=tmp_path,
        env={},
        record=lambda name, value: evidence.update({name: value}),
    )
    app.name = "chatgpt"
    app.version = "pinned"
    app.executable = "app"
    monkeypatch.setattr(
        windows_app.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("no launch")),
    )
    with pytest.raises(OSError, match="no launch"):
        app.__enter__()
    assert evidence["windows-sandbox-prelaunch.json"]["config"]["exists"] is False


def test_windows_app_log_preserves_and_timestamps_every_line(tmp_path):
    log = tmp_path / "app.log"
    windows_app._capture_app_log(io.StringIO("first\nsecond\n"), log, [], threading.Condition())
    lines = log.read_text().splitlines()
    assert len(lines) == 2
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\S+ first", lines[0])
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\S+ second", lines[1])


def test_app_log_records_sandbox_and_app_server_process_timestamps(tmp_path, monkeypatch):
    monkeypatch.setattr(
        windows_app,
        "_windows_process_start",
        lambda pid: "2026-11-25T00:00:00+00:00",
        raising=False,
    )
    lines = (
        "[StdioConnection] stdio_transport_spawned pid=42\n"
        "[AppServerConnection] response_routed method=windowsSandbox/readiness errorCode=null\n"
        "[AppServerConnection] response_routed method=windowsSandbox/setupStart errorCode=null\n"
        "[AppServerConnection] app_server_connection.state_changed cause=start_process hostId=remote\n"
        "[AppServerConnection] app_server_connection.state_changed cause=stop_process hostId=local\n"
        "[AppServerConnection] app_server_connection.state_changed cause=start_process hostId=local\n"
        "[AppServerConnection] app_server_connection.state_changed hostId=local initialized=trueX next=connected\n"
        "[AppServerConnection] app_server_connection.state_changed hostId=local initialized=true next=connectedX\n"
        "[AppServerConnection] app_server_connection.state_changed cause=post_initialize_connection_state hostId=remote initialized=true next=connected\n"
        "[AppServerConnection] app_server_connection.state_changed cause=post_initialize_connection_state hostId=local initialized=true next=connected\n"
        "[AppServerConnection] response_routed method=model/list errorCode=BAD_REQUEST\n"
        "[AppServerConnection] response_routed method=model/listExtra errorCode=null\n"
        "[AppServerConnection] response_routed method=model/list errorCode=nullish\n"
        "[AppServerConnection] response_routed method=model/list errorCode=null\n"
        "Failed to load Windows sandbox readiness: Hosted AppServerManager identity is not ready\n"
    )
    events = []
    windows_app._capture_app_log(
        io.StringIO(lines), tmp_path / "app.log", events, threading.Condition()
    )
    assert [event["event"] for event in events] == [
        "stdio-transport-spawn",
        "readiness",
        "setupStart",
        "stop_process",
        "start_process",
        "connected",
        "model/list",
        "identity-not-ready",
    ]
    assert events[0]["pid"] == 42
    assert events[0]["process_started_at"] == "2026-11-25T00:00:00+00:00"
    assert all(
        datetime.fromisoformat(event["observed_at"]).utcoffset().total_seconds() == 0
        for event in events
    )


def test_windows_app_log_redacts_service_principal_secret(monkeypatch):
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "private-client-secret")
    session = SimpleNamespace(redact=lambda text: text)
    result = windows_app._redact_app_log(session, "error private-client-secret")
    assert result == "error <redacted>"


def test_windows_uia_timing_excludes_input_and_records_failed_actions():
    helper = (
        "import json,sys\n"
        "print(json.dumps({'event':'ready','metrics':{'init_ms':0}}),flush=True)\n"
        "for line in sys.stdin:\n"
        " print(json.dumps({'ok':False, 'output':'', 'error':'failed'}),flush=True)\n"
    )
    app = object.__new__(windows_app.WindowsApp)
    app.proc = SimpleNamespace(poll=lambda: None, pid=123)
    app.name = "chatgpt"
    app.timings = []
    app.uia_process = windows_app.WindowsUIAProcess([sys.executable, "-u", "-c", helper])
    try:
        with pytest.raises(AssertionError, match="UI Automation type failed"):
            app.uia("type", "private-prompt", "enter")
    finally:
        app.uia_process.close()
    assert len(app.timings) == 1
    assert app.timings[0]["action"] == "type"
    assert app.timings[0]["seconds"] >= 0
    assert "private-prompt" not in repr(app.timings)


def test_windows_wait_uses_native_condition_and_returns_tree():
    app = object.__new__(windows_app.WindowsApp)
    app.name = "chatgpt"
    actions = []
    app.uia = lambda *args: actions.append(args) or "2"
    app.read_tree = lambda: [["ControlType.Text", "Ready", ""]]
    assert app.wait_for("ControlType.Text", "Ready", timeout=17) == [
        ["ControlType.Text", "Ready", ""]
    ]
    assert actions == [("wait", "ControlType.Text", "Ready", "17")]


def test_windows_wait_requires_the_returned_tree_to_contain_the_target():
    app = object.__new__(windows_app.WindowsApp)
    app.name = "chatgpt"
    app.uia = lambda *args: "1"
    app.read_tree = lambda: []
    app.session = SimpleNamespace(record=lambda *args: None)
    with pytest.raises(AssertionError, match="disappeared"):
        app.wait_for("ControlType.Text", "Ready")


def test_windows_wait_records_poll_count_without_control_text():
    helper = (
        "import json,sys\n"
        "print(json.dumps({'event':'ready','metrics':{'init_ms':0}}),flush=True)\n"
        "for line in sys.stdin:\n"
        " print(json.dumps({'ok':True, 'output':'2', 'error':''}),flush=True)\n"
    )
    app = object.__new__(windows_app.WindowsApp)
    app.proc = SimpleNamespace(poll=lambda: None, pid=123)
    app.timings = []
    app.name = "chatgpt"
    app.read_tree = lambda: [["ControlType.Text", "private-label", ""]]
    app.uia_process = windows_app.WindowsUIAProcess([sys.executable, "-u", "-c", helper])
    try:
        app.wait_for("ControlType.Text", "private-label")
    finally:
        app.uia_process.close()
    assert app.timings[0]["action"] == "wait"
    assert app.timings[0]["polls"] == 2
    assert app.timings[0]["seconds"] >= 0
    assert "private-label" not in repr(app.timings)


@pytest.mark.parametrize("failure", [None, "configure", "identity"])
def test_token_profile_removes_bypasses_and_stops_on_failure(tmp_path, failure):
    calls = []
    session = SimpleNamespace(
        env={
            "HOME": str(tmp_path),
            "DATABRICKS_BEARER": "ci-token",
            "DATABRICKS_BEARER_COMMAND": "credential-broker",
            "DATABRICKS_CONFIG_FILE": "other-profile-file",
        },
        which=lambda name: tmp_path / name,
    )

    def run(*args, **kwargs):
        assert session.env == {"HOME": str(tmp_path)}
        assert kwargs["binary"] == tmp_path / "databricks"
        assert "ci-token" not in args
        calls.append(args)
        if args[0] == "configure":
            assert kwargs["input_text"] == "ci-token\n"
            if failure == "configure":
                raise AssertionError("Profile setup failed")
        elif failure == "identity":
            raise AssertionError("Identity verification failed")

    session.run = run
    if failure:
        with pytest.raises(AssertionError, match="failed"):
            codex_desktop.configure_databricks_token_profile(session, "https://workspace.example")
    else:
        profile = codex_desktop.configure_databricks_token_profile(
            session, "https://workspace.example"
        )
        assert calls == [
            ("configure", "--host", "https://workspace.example", "--profile", profile),
            ("current-user", "me", "--profile", profile),
        ]
    assert len(calls) == (1 if failure == "configure" else 2)


@pytest.mark.parametrize("state", ["ready", "recovers", "fails"])
def test_windows_readiness_recheck_is_visible_and_bounded(state):
    error = [["ControlType.Text", "Couldn’t check Windows setup", ""]]
    tree = [] if state == "ready" else list(error)
    clicks = []

    def click(role, label):
        clicks.append((role, label))
        if state == "recovers":
            tree.clear()

    app = SimpleNamespace(
        read_tree=lambda: tree, click=click, session=SimpleNamespace(record=lambda *args: None)
    )
    if state == "fails":
        with pytest.raises(AssertionError, match="readiness still failed"):
            codex_desktop.finish_windows_readiness_check(app, timeout=0)
    else:
        codex_desktop.finish_windows_readiness_check(app, timeout=0)
    assert clicks == ([] if state == "ready" else [("ControlType.Button", "Try again")])


@pytest.mark.parametrize("state", ["banner", "recovers", "repeats", "stuck", "elevated"])
def test_windows_setup_finishes_one_transient_readiness_error(state, monkeypatch, tmp_path):
    monkeypatch.setattr(codex_desktop.time, "sleep", lambda seconds: None)
    if state == "elevated":
        (tmp_path / ".codex").mkdir()
        (tmp_path / ".codex/config.toml").write_text('[windows]\nsandbox = "elevated"\n')
    banner = "Finish Windows setup to continue"
    error = [["ControlType.Text", "Couldn’t check Windows setup", ""]]
    ready = [["ControlType.Text", banner, ""]]
    # Trees read in order, then the last one forever; Try again clears the error once.
    first, rest = {
        "banner": ([], ready),
        "recovers": ([error, []], ready),
        "repeats": ([error, []], error),
        "stuck": ([], []),
        # The app can finish setup after the recheck without ever showing the banner.
        "elevated": ([error, []], []),
    }[state]
    trees = iter(first)
    clicks = []
    evidence = {}
    app = SimpleNamespace(
        name="chatgpt",
        read_tree=lambda: next(trees, rest),
        click=lambda role, label: clicks.append((role, label)),
        session=SimpleNamespace(
            home=tmp_path, record=lambda name, value: evidence.update({name: value})
        ),
    )
    if state in ("repeats", "stuck"):
        with pytest.raises(AssertionError, match="never showed"):
            codex_desktop.wait_for_windows_setup(app, timeout=0.05)
        assert evidence["chatgpt-tree.json"]["waiting_for"] == ["ControlType.Text", banner]
        assert evidence["chatgpt-tree.json"]["tree"] == rest
    else:
        assert codex_desktop.wait_for_windows_setup(app, timeout=0.05) == (state != "elevated")
    assert clicks == ([("ControlType.Button", "Try again")] if first else [])


@pytest.mark.parametrize("mode", [None, "unelevated", "elevated"])
def test_windows_setup_requires_the_native_elevated_mode(tmp_path, mode):
    config = tmp_path / ".codex/config.toml"
    config.parent.mkdir()
    config.write_text(f'[windows]\nsandbox = "{mode}"\n' if mode else "")
    evidence = {}
    app = SimpleNamespace(
        read_tree=lambda: [],
        session=SimpleNamespace(
            home=tmp_path, record=lambda name, value: evidence.update({name: value})
        ),
    )
    if mode == "elevated":
        codex_desktop.wait_for_windows_sandbox(app, timeout=0)
        assert evidence["windows-sandbox.json"] == {"mode": "elevated"}
    else:
        with pytest.raises(AssertionError, match="never completed elevated Windows sandbox setup"):
            codex_desktop.wait_for_windows_sandbox(app, timeout=0)
        assert "chatgpt-tree.json" in evidence


@pytest.mark.parametrize("initial", [None, "[windows"])
@pytest.mark.parametrize("recovers", [False, True])
def test_windows_setup_waits_for_missing_or_partial_config(
    tmp_path, monkeypatch, initial, recovers
):
    config = tmp_path / ".codex/config.toml"
    config.parent.mkdir()
    if initial is not None:
        config.write_text(initial)
    elapsed = [0]

    def sleep(seconds):
        elapsed[0] += seconds
        if recovers:
            config.write_text('[windows]\nsandbox = "elevated"\n')

    monkeypatch.setattr(codex_desktop.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(codex_desktop.time, "sleep", sleep)
    app = SimpleNamespace(
        read_tree=lambda: [],
        session=SimpleNamespace(home=tmp_path, record=lambda *args: None),
    )
    if recovers:
        codex_desktop.wait_for_windows_sandbox(app, timeout=2)
        assert elapsed[0] == 1
    else:
        with pytest.raises(AssertionError, match="never completed elevated Windows sandbox setup"):
            codex_desktop.wait_for_windows_sandbox(app, timeout=2)
        assert elapsed[0] == 2


@pytest.mark.parametrize(
    ("text_role", "button_role"),
    [("AXStaticText", "AXButton"), ("ControlType.Text", "ControlType.Button")],
)
@pytest.mark.parametrize("invalid", [None, "server", "tool", "query", "echoed_query", "ambiguous"])
def test_gui_approves_only_the_requested_search(monkeypatch, text_role, button_role, invalid):
    approval = codex_desktop.SEARCH_APPROVAL
    if invalid == "server":
        approval = approval.replace(MCP_SERVER, "other-server")
    if invalid == "tool":
        approval = approval.replace('"web_search"', '"other-tool"')
    tree = [
        [text_role, "", approval],
        [text_role, "", "query"],
        [text_role, "", "other query" if invalid in {"query", "echoed_query"} else QUERY],
        [button_role, "Allow once", ""],
    ]
    if invalid == "ambiguous":
        tree.append([button_role, "Allow once", ""])
    if invalid == "echoed_query":
        tree.insert(0, [text_role, "", QUERY])
    clicks = []

    def click(role, label):
        clicks.append((role, label))
        tree[:] = [[text_role, "", MARKER]]

    app = SimpleNamespace(
        read_tree=lambda: tree,
        click=click,
        session=SimpleNamespace(record=lambda *args: None),
    )
    monkeypatch.setattr(codex_desktop.time, "sleep", lambda seconds: None)
    if invalid:
        with pytest.raises(AssertionError, match="Unexpected tool approval"):
            codex_desktop.wait_for_skill_answer(app, QUERY, MARKER, text_role, button_role, 1)
        assert not clicks
    else:
        codex_desktop.wait_for_skill_answer(app, QUERY, MARKER, text_role, button_role, 1)
        assert clicks == [(button_role, "Allow once")]


@pytest.mark.parametrize(
    ("kind", "role", "text", "submitted"),
    [
        ("response_item", "user", "Use the skill.\n", True),
        ("response_item", "assistant", "Use the skill.", False),
        ("response_item", "user", "Another prompt.", False),
        ("event_msg", "user", "Use the skill.", False),
    ],
)
def test_submission_requires_the_native_user_prompt(monkeypatch, kind, role, text, submitted):
    sessions = {
        "desktop": [
            {
                "type": kind,
                "payload": {
                    "type": "message",
                    "role": role,
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        ]
    }
    monkeypatch.setattr(codex_desktop, "agent_sessions", lambda *args: sessions)
    evidence = {}
    session = SimpleNamespace(record=lambda name, value: evidence.update({name: value}))
    if submitted:
        codex_desktop.wait_for_submitted_prompt(session, "Use the skill.", timeout=0)
    else:
        with pytest.raises(AssertionError, match="never recorded the submitted prompt"):
            codex_desktop.wait_for_submitted_prompt(session, "Use the skill.", timeout=0)
        assert evidence["agent-sessions.json"] == sessions


@pytest.fixture
def records():
    return [
        {
            "type": "session_meta",
            "payload": {"originator": "Codex Desktop", "model_provider": "Databricks"},
        },
        {"type": "turn_context", "payload": {"turn_id": "turn-1", "model": MODEL}},
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": "turn-1",
                "item": {
                    "type": "McpToolCall",
                    "id": "call-1",
                    "server": MCP_SERVER,
                    "tool": "web_search",
                    "arguments": {"query": QUERY},
                    "status": "completed",
                    "result": {"content": [{"type": "text", "text": "Databricks documentation"}]},
                },
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "turn-1",
                "last_agent_message": MARKER,
            },
        },
    ]


def test_skill_values_are_only_in_the_reference_file(tmp_path):
    skill = write_search_skill(tmp_path, QUERY, MARKER)

    assert skill == tmp_path / ".codex/skills" / SKILL_NAME
    instructions = (skill / "SKILL.md").read_text()
    assert "references/task.md" in instructions and MCP_SERVER in instructions
    assert QUERY not in instructions and MARKER not in instructions
    assert (skill / "references/task.md").read_text() == (
        f"Query: {QUERY}\nCompletion token: {MARKER}\n"
    )


def test_native_mcp_receipts_allow_extra_successful_calls(records):
    assert_skill_mcp_turn(records, QUERY, MARKER, MODEL)
    extra_call = copy.deepcopy(records[2])
    extra_call["payload"]["item"]["id"] = "call-2"
    records.insert(3, extra_call)
    assert_skill_mcp_turn(records, QUERY, MARKER, MODEL)


@pytest.fixture
def desktop_session(monkeypatch, records):
    child = copy.deepcopy(records)
    child[0]["payload"]["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": "root"}}}
    sessions = {"child.jsonl": child, "root.jsonl": records}
    evidence = {}
    session = SimpleNamespace(
        sessions=sessions,
        evidence=evidence,
        record=lambda name, value: evidence.update({name: value}),
    )
    monkeypatch.setattr(codex_desktop, "agent_sessions", lambda *args: sessions)
    return session


def test_desktop_root_allows_child_rollouts_and_records_all_evidence(desktop_session):
    codex_desktop.assert_desktop_skill_mcp_session(desktop_session, QUERY, MARKER, MODEL)
    assert desktop_session.evidence["agent-sessions.json"] == desktop_session.sessions


@pytest.mark.parametrize("root_count", [0, 2])
def test_missing_or_ambiguous_desktop_roots_fail(desktop_session, root_count):
    if root_count == 0:
        del desktop_session.sessions["root.jsonl"]
    else:
        desktop_session.sessions["another-root.jsonl"] = desktop_session.sessions["root.jsonl"]
    with pytest.raises(AssertionError, match="Expected one desktop root session"):
        codex_desktop.assert_desktop_skill_mcp_session(desktop_session, QUERY, MARKER, MODEL)


@pytest.mark.parametrize(
    ("index", "changes"),
    [
        (0, {"originator": "codex_cli_rs"}),
        (0, {"model_provider": "OpenAI"}),
        (1, {"model": "another-model"}),
        (1, {"turn_id": "another-turn"}),
        (2, {"turn_id": "earlier-turn"}),
        (2, {"type": "item_started"}),
        (3, {"type": "agent_message"}),
        (3, {"last_agent_message": f"I could not finish {MARKER}"}),
    ],
)
def test_wrong_origin_model_turn_or_answer_fails(records, desktop_session, index, changes):
    records[index]["payload"].update(changes)
    with pytest.raises(AssertionError):
        codex_desktop.assert_desktop_skill_mcp_session(desktop_session, QUERY, MARKER, MODEL)


@pytest.mark.parametrize(
    "changes",
    [
        {"type": "WebSearch"},
        {"server": "another-server"},
        {"tool": "another-tool"},
        {"arguments": {"query": "an unrelated query"}},
        {"status": "inProgress"},
        {"status": "failed"},
        {"error": {"message": "permission denied"}},
        {"result": None},
        {"result": {"isError": True, "content": [{"type": "text", "text": "error"}]}},
        {"result": {"content": []}},
        {"result": {"content": [{"type": "text", "text": " "}]}},
    ],
)
def test_every_matching_mcp_receipt_must_succeed(records, changes):
    records[2]["payload"]["item"].update(changes)
    with pytest.raises(AssertionError):
        assert_skill_mcp_turn(records, QUERY, MARKER, MODEL)


def test_success_cannot_hide_another_failed_matching_call(records):
    failed_call = copy.deepcopy(records[2])
    failed_call["payload"]["item"].update(id="failed-call", status="failed")
    records.insert(3, failed_call)
    with pytest.raises(AssertionError):
        assert_skill_mcp_turn(records, QUERY, MARKER, MODEL)


def test_final_answer_alone_cannot_prove_tool_use(records, desktop_session):
    del records[2]
    with pytest.raises(AssertionError, match="No native web_search receipt"):
        codex_desktop.assert_desktop_skill_mcp_session(desktop_session, QUERY, MARKER, MODEL)
