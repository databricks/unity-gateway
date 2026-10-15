"""Claude status line for smart-routed launches: wrapping and the ON/OFF state."""

from __future__ import annotations

import io
import json
import shlex
import sys

import pytest

from ucode.constants import ENABLE_SMART_ROUTING_ENV_VAR, ENABLE_SUBAGENT_ROUTING_ENV_VAR
from ucode.smart_routing import status_line
from ucode.smart_routing.session_env import SESSION_ENV_VAR

ON = status_line._ON
OFF = status_line._OFF


@pytest.fixture
def routed(tmp_path, monkeypatch):
    session = tmp_path / "env.json"
    session.write_text("{}")
    monkeypatch.delenv("SMART_ROUTER_CONFIG_VERSION", raising=False)
    monkeypatch.delenv(ENABLE_SMART_ROUTING_ENV_VAR, raising=False)
    monkeypatch.setenv(ENABLE_SUBAGENT_ROUTING_ENV_VAR, "1")
    monkeypatch.setenv(SESSION_ENV_VAR, str(session))
    return session


def test_segment_follows_session_toggles(routed):
    assert status_line.segment() == f"smart router {ON}"
    routed.write_text(
        json.dumps({ENABLE_SMART_ROUTING_ENV_VAR: "0", ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0"})
    )
    assert status_line.segment() == f"smart router {OFF}"
    routed.write_text(json.dumps({ENABLE_SMART_ROUTING_ENV_VAR: "1"}))
    assert status_line.segment() == f"smart router {ON}"


def test_effective_status_line_uses_settings_precedence(tmp_path):
    local, project, user = (tmp_path / name for name in ("local.json", "project.json", "user.json"))
    user.write_text(
        json.dumps({"statusLine": {"type": "command", "command": "user", "padding": 0}})
    )
    paths = [local, project, user]

    assert status_line.effective_status_line({}, paths) == {
        "type": "command",
        "command": "user",
        "padding": 0,
    }
    project.write_text(json.dumps({"statusLine": {"type": "command", "command": "project"}}))
    assert status_line.effective_status_line({}, paths)["command"] == "project"
    launch = {"statusLine": {"type": "command", "command": "launch"}}
    assert status_line.effective_status_line(launch, paths)["command"] == "launch"
    # A higher-precedence status line that is not a command still hides the lower ones.
    local.write_text(json.dumps({"statusLine": {"type": "static"}}))
    assert status_line.effective_status_line({}, paths) is None


def test_effective_status_line_never_wraps_its_own_line(tmp_path):
    own = status_line.launch_setting("/ug/python", {"type": "command", "command": "user"})
    assert status_line.effective_status_line({"statusLine": own}, []) is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell quoting")
def test_launch_setting_runs_the_module_with_the_quoted_base():
    base = {"type": "command", "command": "bash '/my dir/line.sh'", "padding": 2}

    setting = status_line.launch_setting("/ug venv/python", base)

    assert setting["type"] == "command"
    assert setting["padding"] == 2
    assert setting["refreshInterval"] == status_line.REFRESH_INTERVAL_SECONDS
    assert shlex.split(setting["command"]) == [
        "/ug venv/python",
        "-m",
        status_line.MODULE,
        "bash '/my dir/line.sh'",
    ]
    assert shlex.split(status_line.launch_setting("/py", None)["command"]) == [
        "/py",
        "-m",
        status_line.MODULE,
    ]


def test_launch_setting_keeps_a_shorter_user_refresh_interval():
    def interval(value):
        base = {"type": "command", "command": "user", "refreshInterval": value}
        return status_line.launch_setting("/py", base)["refreshInterval"]

    assert interval(1) == 1
    assert interval(60) == status_line.REFRESH_INTERVAL_SECONDS
    assert interval(True) == status_line.REFRESH_INTERVAL_SECONDS


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell base command")
def test_main_appends_the_state_to_the_base_line(routed, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"model": {"id": "opus"}}'))

    assert status_line.main(["cat >/dev/null; printf 'base line\\n'"]) == 0

    assert capsys.readouterr().out == f"base line │ smart router {ON}\n"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell base command")
@pytest.mark.parametrize("command", ["exit 3", "printf ''"])
def test_main_shows_only_the_state_without_a_base_line(routed, monkeypatch, capsys, command):
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))

    assert status_line.main([command]) == 0
    assert status_line.main([]) == 0

    assert capsys.readouterr().out == f"smart router {ON}\n" * 2
