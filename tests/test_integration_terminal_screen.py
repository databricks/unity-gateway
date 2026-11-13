"""Unit checks for the integration harness's virtual terminal screen."""

import json
import sys
from contextlib import nullcontext
from pathlib import Path

import pyte
import pytest

from tests.integration.utils.harness import UserSession
from tests.integration.utils.terminal import AgentTerminal, TerminalProcess, TerminalScreen


@pytest.mark.parametrize("fail", [False, True])
def test_terminal_preserves_redacted_proxy_diagnostics_outside_screen(tmp_path, fail):
    root, project = tmp_path / "session", tmp_path / "project"
    root.mkdir()
    project.mkdir()
    session = UserSession(root, project, Path(sys.executable), tmp_path / "artifacts")
    session.env["DATABRICKS_BEARER"] = "test-secret-bearer"
    command = [
        str(session.binary),
        "-c",
        "import os, sys; "
        "from ucode.gateway_proxy import log_proxy_diagnostic; "
        "print('prompt-ready', flush=True); input(); "
        "log_proxy_diagnostic('request_start', request_id=os.environ['DATABRICKS_BEARER']); "
        "print('ordinary stderr remains visible', file=sys.stderr, flush=True); "
        "print('diagnostic-recorded', flush=True); input()",
    ]
    expected = (
        pytest.raises(RuntimeError, match="simulated test failure") if fail else nullcontext()
    )
    with expected:
        with TerminalProcess(session, "ug", command, "diagnostic-terminal") as terminal:
            terminal.wait_for(lambda screen: "prompt-ready" in screen, "initial prompt")
            terminal.send("\r", "emit proxy diagnostic")
            terminal.wait_for(lambda screen: "diagnostic-recorded" in screen, "diagnostic emitted")
            assert "ordinary stderr remains visible" in terminal.visible
            assert "[ucode-relay]" not in terminal.visible
            if fail:
                raise RuntimeError("simulated test failure")

    recorded = json.loads((session.artifacts / "diagnostic-terminal.json").read_text())
    assert "[ucode-relay]" not in recorded["transcript"]
    assert "test-secret-bearer" not in json.dumps(recorded)
    event = json.loads(recorded["proxy_diagnostics"].strip().removeprefix("[ucode-relay] "))
    assert event == {"event": "request_start", "request_id": "<redacted>"}


def _screen() -> TerminalScreen:
    return TerminalScreen(10, 2, lambda data: None)


def test_display_renders_text_like_pyte():
    screen = _screen()
    pyte.Stream(screen).feed("hi 世界")

    assert screen.display[0] == "hi 世界" + " " * 3
    assert screen.display == pyte.Screen.display.fget(screen)


def test_display_treats_an_empty_cell_as_a_space():
    screen = _screen()
    pyte.Stream(screen).feed("ab")
    screen.buffer[0][1] = screen.buffer[0][1]._replace(data="")

    assert screen.display[0] == "a" + " " * 9


class _TaskWaitTerminal:
    wait_for_task = AgentTerminal.wait_for_task

    def __init__(self, session, screens):
        self.agent = "claude"
        self.session = session
        self.screens = screens
        self.sent = []

    def approve_windows_command(self, screen):
        return False

    def send(self, keys, reason):
        self.sent.append((keys, reason))

    def wait_for(self, predicate, description, timeout):
        assert not any(predicate(screen) for screen in self.screens[:-1])
        assert predicate(self.screens[-1]), description


class _Task:
    filename = "input.txt"

    def __init__(self):
        self.asserted = False

    def completed(self, session, agent):
        return True

    def assert_completed(self, session, agent):
        self.asserted = True


def test_wait_for_task_custom_evidence_keeps_permission_handling(tmp_path):
    session = type("Session", (), {"cwd": tmp_path / "project"})()
    dialog = f'Do you want to proceed?\nfind {tmp_path} -name "input.txt" 2>/dev/null\n❯ 1. Yes'
    terminal = _TaskWaitTerminal(session, [dialog, "ready"])
    task = _Task()
    terminal.wait_for_task(task, completion=lambda: True)
    assert terminal.sent == [("\r", "allow read-only search for input.txt")]
    assert not task.asserted


def test_wait_for_task_default_still_asserts_parent_answer():
    terminal = _TaskWaitTerminal(None, ["ready"])
    task = _Task()
    terminal.wait_for_task(task)
    assert task.asserted


def test_wait_for_task_custom_evidence_does_not_hide_api_errors():
    terminal = _TaskWaitTerminal(None, ["unexpected status 401"])
    with pytest.raises(AssertionError, match="terminal API error"):
        terminal.wait_for_task(_Task(), completion=lambda: True)
