"""Offline protocol and session isolation checks for the experimental Codex footer."""

import io
import json
import os
import queue
import subprocess
import sys
import threading
import time

import pytest

from ucode.constants import ENABLE_SMART_ROUTING_ENV_VAR, ENABLE_SUBAGENT_ROUTING_ENV_VAR
from ucode.smart_routing import codex_statusline as statusline
from ucode.smart_routing.codex_statusline import LOGS_DETAILS_HINT
from ucode.smart_routing.route_validation import VALIDATION_TIMEOUT_SECONDS
from ucode.smart_routing.session_env import (
    SESSION_ENV_VAR,
    set_session_environment,
    start_session,
    validation_dismissed_path,
    validation_result_path,
)


def _labels(tree):
    if isinstance(tree, str):
        return tree
    return " ".join(" ".join(_labels(child) for child in tree.get("children", [])).split())


def _read_lines(stream, replies):
    for line in stream:
        replies.put(line)


def test_launch_config_pins_session_without_credentials(tmp_path):
    controls = tmp_path / "controls with spaces.json"
    config = statusline.launch_config(controls)
    server = config["mcp_servers"][statusline.SERVER_NAME]
    assert server["command"] == sys.executable
    assert server["args"] == ["-P", "-X", "utf8", "-m", "ucode.smart_routing.codex_statusline"]
    assert server["env"] == {SESSION_ENV_VAR: str(controls)}
    assert config["features.tui_extensions_statusline"] is True
    assert config["tui.status_line_provider"] == {
        "server": statusline.SERVER_NAME,
        "tool": statusline.TOOL_NAME,
    }


def test_terminal_config_preserves_features_without_server_settings(tmp_path):
    config = statusline.launch_config(tmp_path / "controls.json")
    config["server_only"] = "private"
    config["features"] = {"existing_feature": True}
    config["features.hooks"] = True
    config["features.multi_agent_v2.non_code_mode_only"] = False
    assert statusline.tui_config(config) == {
        "features": {"existing_feature": True},
        "features.hooks": True,
        "features.multi_agent_v2.non_code_mode_only": False,
        "features.tui_extensions_statusline": True,
        "tui.status_line_provider": {
            "server": statusline.SERVER_NAME,
            "tool": statusline.TOOL_NAME,
        },
    }
    assert statusline.tui_config({}) == {}


def test_statusline_tool_is_visible_to_the_app_only():
    (tool,) = statusline.respond("tools/list", {})["tools"]
    assert tool["_meta"]["ui"]["visibility"] == ["app"]
    assert tool["_meta"]["openai/ui"]["entrypoints"] == [
        {"type": "statusline", "resourceUri": statusline.RESOURCE_URI}
    ]


@pytest.mark.parametrize(
    "state",
    [
        None,
        {"thread": None},
        {"thread": []},
        {"thread": {"model": None}},
        {"thread": {"model": []}},
        {"thread": {"workspace": None}},
        {"thread": {"usage": None}},
        {"thread": {"usage": {"context": None}}},
    ],
)
def test_nullable_snapshots_keep_footer_available(tmp_path, monkeypatch, state):
    controls = tmp_path / "controls.json"
    controls.write_text("{}")
    monkeypatch.setenv(SESSION_ENV_VAR, str(controls))
    reply = statusline.respond(
        "resources/read",
        {"uri": statusline.RESOURCE_URI, "_meta": {statusline.STATE_META_KEY: state}},
    )
    labels = _labels(json.loads(reply["contents"][0]["text"]))
    assert "smart router ON" in labels
    assert "Codex" in labels


def test_footer_tracks_session_toggles_and_current_thread(tmp_path, monkeypatch):
    monkeypatch.setenv(SESSION_ENV_VAR, str(tmp_path / "controls.json"))
    (tmp_path / "controls.json").write_text("{}")
    state = {
        "thread": {
            "model": {"display_name": "GPT Test", "reasoning_level": "high"},
            "workspace": {"cwd": "/projects/my-app"},
            "usage": {"context": {"used_percentage": 25}},
        }
    }
    labels = _labels(statusline.render(state))
    assert "smart router ON" in labels
    assert "GPT Test high" in labels
    assert "my-app" in labels
    assert "75% context left" in labels
    set_session_environment(
        {ENABLE_SMART_ROUTING_ENV_VAR: "0", ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0"}
    )
    assert "smart router OFF" in _labels(statusline.render(state))
    set_session_environment(
        {ENABLE_SMART_ROUTING_ENV_VAR: "0", ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1"}
    )
    state["thread"]["model"] = {"display_name": "Other thread"}
    assert "smart router ON" in _labels(statusline.render(state))
    assert "Other thread" in _labels(statusline.render(state))
    assert "GPT Test" not in _labels(statusline.render(state))


def test_footer_handles_missing_snapshot_and_windows_paths(tmp_path, monkeypatch):
    controls = tmp_path / "controls.json"
    controls.write_text("{}")
    monkeypatch.setenv(SESSION_ENV_VAR, str(controls))
    assert "Codex" in _labels(statusline.render({}))
    assert "my-app" in _labels(
        statusline.render({"thread": {"workspace": {"cwd": r"C:\projects\my-app"}}})
    )


@pytest.mark.parametrize(
    ("enabled", "label", "background"), [(True, "ON", "green"), (False, "OFF", "#808080")]
)
def test_footer_matches_claude_badges(tmp_path, monkeypatch, enabled, label, background):
    controls = tmp_path / "controls.json"
    controls.write_text(
        json.dumps(
            {
                ENABLE_SMART_ROUTING_ENV_VAR: "0",
                ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1" if enabled else "0",
            }
        )
    )
    monkeypatch.setenv(SESSION_ENV_VAR, str(controls))
    indicator = statusline.render({})["children"][0]
    assert _labels(indicator) == f"smart router {label}"
    badge = indicator["children"][1]
    assert badge["props"] == {"gap": 0, "background": background}
    assert badge["children"] == [
        {"type": "text", "props": {"color": "black", "weight": "bold"}, "children": [f" {label} "]}
    ]


@pytest.mark.parametrize("contents", [None, "not-json", "[]", '{"unrecognized": "1"}'])
def test_unreadable_controls_show_unknown_and_recover(tmp_path, monkeypatch, contents):
    controls = tmp_path / "controls.json"
    monkeypatch.setenv(SESSION_ENV_VAR, str(controls))
    if contents is not None:
        controls.write_text(contents)
    assert "smart router UNKNOWN" in _labels(statusline.render({}))
    controls.write_text("{}")
    assert "smart router ON" in _labels(statusline.render({}))


UNSUPPORTED = {"routing_disabled": True, "validation_ms": 120}
FAILED = {"routing_disabled": True, "validation_failed": True, "validation_ms": 90}


def _controls(tmp_path, monkeypatch, enabled):
    controls = tmp_path / "controls.json"
    controls.write_text(
        json.dumps(
            {
                ENABLE_SMART_ROUTING_ENV_VAR: "0",
                ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1" if enabled else "0",
            }
        )
    )
    monkeypatch.setenv(SESSION_ENV_VAR, str(controls))
    return controls


@pytest.mark.parametrize(
    ("enabled", "validation", "dismissed", "expected"),
    [
        pytest.param(
            False,
            UNSUPPORTED,
            False,
            (
                "ERROR",
                "⚠ smart routing unsupported",
                LOGS_DETAILS_HINT,
            ),
            id="startup-unsupported",
        ),
        pytest.param(
            False,
            UNSUPPORTED,
            True,
            ("ERROR", LOGS_DETAILS_HINT),
            id="startup-unsupported-after-turn",
        ),
        pytest.param(
            False,
            FAILED,
            False,
            (
                "ERROR",
                "⚠ Smart Router couldn't validate this session's model list",
                LOGS_DETAILS_HINT,
            ),
            id="startup-failed",
        ),
        pytest.param(
            False,
            FAILED,
            True,
            ("ERROR", LOGS_DETAILS_HINT),
            id="startup-failed-after-turn",
        ),
        pytest.param(True, {"routing_disabled": False}, False, ("ON",), id="supported"),
        pytest.param(False, None, False, ("OFF",), id="no-result"),
    ],
)
def test_footer_shows_the_validation_state(
    tmp_path, monkeypatch, enabled, validation, dismissed, expected
):
    controls = _controls(tmp_path, monkeypatch, enabled)
    if validation is not None:
        validation_result_path(controls).write_text(json.dumps(validation))
    if dismissed:
        validation_dismissed_path(controls).touch()
    labels = _labels(statusline.render({}))
    assert labels.startswith(" ".join(("smart router", *expected)))


@pytest.mark.parametrize(
    ("age_seconds", "expected"),
    [
        (1, "smart router · Codex"),
        (VALIDATION_TIMEOUT_SECONDS + 0.5, "smart router · Codex"),
        (statusline.VALIDATION_PENDING_SECONDS + 1, "smart router ON · Codex"),
    ],
    ids=["pending", "pending-past-the-check-deadline", "timed-out"],
)
def test_footer_has_no_badge_while_the_startup_check_is_pending(
    tmp_path, monkeypatch, age_seconds, expected
):
    controls = _controls(tmp_path, monkeypatch, True)
    result = validation_result_path(controls)
    result.write_text(json.dumps({"pending": True}))
    past = time.time() - age_seconds
    os.utime(result, (past, past))
    assert _labels(statusline.render({})) == expected


def test_footer_does_not_retry_blocked_file_reads(tmp_path, monkeypatch):
    def blocked(*_args, **_kwargs):
        raise PermissionError("locked")

    monkeypatch.setenv(SESSION_ENV_VAR, str(tmp_path / "controls.json"))
    monkeypatch.setattr(statusline.Path, "read_text", blocked)
    monkeypatch.setattr(
        "ucode.smart_routing.session_env.time.sleep", lambda _: pytest.fail("slept")
    )
    assert "smart router UNKNOWN" in _labels(statusline.render({}))


def test_missing_session_controls_never_uses_inherited_on_state(monkeypatch):
    monkeypatch.delenv(SESSION_ENV_VAR, raising=False)
    monkeypatch.setenv(ENABLE_SMART_ROUTING_ENV_VAR, "1")
    assert "smart router UNKNOWN" in _labels(statusline.render({}))


def test_stdio_errors_leave_provider_available():
    requests = [
        "not-json",
        "[]",
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "unknown"}),
        json.dumps(
            {"jsonrpc": "2.0", "id": 2, "method": "resources/read", "params": {"uri": "other"}}
        ),
        json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}),
    ]
    output = io.StringIO()
    statusline.serve(io.StringIO("\n".join(requests) + "\n"), output)
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [reply["error"]["code"] for reply in replies[:-1]] == [-32700, -32600, -32601, -32602]
    assert replies[-1]["result"]["tools"][0]["_meta"]["openai/ui"]["entrypoints"] == [
        {"type": "statusline", "resourceUri": statusline.RESOURCE_URI}
    ]


def test_real_stdio_process_keeps_sessions_separate_and_polls_live_controls():
    processes = []
    try:
        for enabled in (True, False):
            env = os.environ.copy()
            controls = start_session(env)
            if not enabled:
                controls.write_text(
                    json.dumps(
                        {ENABLE_SMART_ROUTING_ENV_VAR: "0", ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0"}
                    )
                )
            server = statusline.launch_config(controls)["mcp_servers"][statusline.SERVER_NAME]
            process = subprocess.Popen(
                [server["command"], *server["args"]],
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            replies = queue.Queue()
            threading.Thread(
                target=_read_lines, args=(process.stdout, replies), daemon=True
            ).start()
            processes.append((process, controls, replies))
        for process, _controls, replies in processes:
            process.stdin.write(
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n"
            )
            process.stdin.flush()
            assert json.loads(replies.get(timeout=10))["result"]["capabilities"] == {
                "tools": {},
                "resources": {},
            }
        for expected in (("ON", "OFF"), ("OFF", "OFF"), ("ON", "OFF")):
            for (process, controls, replies), label in zip(processes, expected, strict=True):
                if controls == processes[0][1]:
                    controls.write_text(
                        json.dumps(
                            {
                                ENABLE_SMART_ROUTING_ENV_VAR: "0",
                                ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1" if label == "ON" else "0",
                            }
                        )
                    )
                process.stdin.write(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "resources/read",
                            "params": {
                                "uri": statusline.RESOURCE_URI,
                                "_meta": {
                                    statusline.STATE_META_KEY: {
                                        "thread": {"workspace": {"cwd": r"C:\projects\项目目录"}}
                                    }
                                },
                            },
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                process.stdin.flush()
                resource = json.loads(replies.get(timeout=10))["result"]["contents"][0]
                assert f"smart router {label}" in _labels(json.loads(resource["text"]))
                assert "项目目录" in _labels(json.loads(resource["text"]))
    finally:
        for process, controls, _replies in processes:
            process.terminate()
            process.communicate(timeout=5)
            controls.unlink()
            controls.parent.rmdir()


def test_footer_names_auto_main_model_only_for_auto_session(tmp_path, monkeypatch):
    controls = tmp_path / "controls.json"
    controls.write_text("{}")
    controls.with_suffix(".auto.json").write_text('{"model":"gpt-two"}')
    monkeypatch.setenv(SESSION_ENV_VAR, str(controls))
    state = {
        "thread": {
            "model": {"id": "ug-auto-codex", "display_name": "Auto", "reasoning_level": "low"}
        }
    }
    assert "Auto (gpt-two)" in _labels(statusline.render(state))
    assert "low" not in _labels(statusline.render(state))
    state["thread"]["model"] = {"id": "gpt-one"}
    assert "Auto (gpt-one)" in _labels(statusline.render(state))
    controls.with_suffix(".auto.json").unlink()
    assert "Auto" not in _labels(statusline.render(state))
    controls.with_suffix(".auto.json").write_text("{")
    assert "Auto" not in _labels(statusline.render(state))
    set_session_environment(
        {ENABLE_SMART_ROUTING_ENV_VAR: "0", ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0"}
    )
    assert "Auto" not in _labels(statusline.render(state))
