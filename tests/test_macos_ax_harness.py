"""Linux-runnable checks for the macOS desktop Accessibility harness in tests/integration."""

import itertools
import json
from types import SimpleNamespace

import pytest

from tests.integration.utils import macos_app
from tests.integration.utils.macos_app import matches


class RecordingSession:
    """The UserSession surface the MacApp helpers use, without a live session."""

    def __init__(self, artifacts):
        self.artifacts = artifacts
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.runs = []

    def run(self, *args, timeout=120, ok=True, binary=None):
        self.runs.append({"argv": [str(binary), *args], "timeout": timeout, "ok": ok})
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def record(self, name, value):
        (self.artifacts / name).write_text(json.dumps(value, indent=2))


def mac_app(session, pid=1813, client=None):
    """A MacApp without an installed bundle; only its helper methods run."""
    app = macos_app.MacApp.__new__(macos_app.MacApp)
    app.session = session
    app.name = "chatgpt"
    app.proc = SimpleNamespace(pid=pid)
    app.client = client
    return app


@pytest.mark.parametrize(
    ("row", "role", "label", "expected"),
    [
        (["AXButton", "Go to ChatGPT", "", ""], "AXButton", "Go to ChatGPT", True),
        (["AXButton", "", "Go to ChatGPT", ""], "AXButton", "Go to ChatGPT", True),
        (["AXButton", "", "", "Go to ChatGPT"], "AXButton", "Go to ChatGPT", True),
        (["AXButton", "Keep setting up", "", ""], "AXButton", "Go to ChatGPT", False),
        (["AXStaticText", "Go to ChatGPT", "", ""], "AXButton", "Go to ChatGPT", False),
        (["AXButton", "Keep setting up", "", ""], "AXButton", None, True),
        (["AXStaticText", "Skip setup?", "", ""], "AXButton", None, False),
    ],
)
def test_ax_rows_match_role_and_label(row, role, label, expected):
    assert matches(row, role, label) is expected


def test_click_waits_and_clicks_through_one_native_command(tmp_path):
    session = RecordingSession(tmp_path / "artifacts")
    app = mac_app(session, pid=1813, client=tmp_path / "macos-ax")

    app.click("AXButton", "Go to ChatGPT", timeout=120)
    app.click("AXTextArea", None)

    explicit, default = session.runs
    assert explicit["argv"] == [
        str(tmp_path / "macos-ax"),
        "click",
        "1813",
        "AXButton",
        "Go to ChatGPT",
        "120",
    ]
    assert explicit["timeout"] == 150
    assert default["argv"] == [
        str(tmp_path / "macos-ax"),
        "click",
        "1813",
        "AXTextArea",
        "",
        "60",
    ]
    assert default["timeout"] == 90


def test_wait_gone_returns_once_the_screen_leaves_the_element(tmp_path, monkeypatch):
    monkeypatch.setattr(macos_app.time, "sleep", lambda seconds: None)
    app = mac_app(RecordingSession(tmp_path / "artifacts"))
    trees = iter(
        [[["AXButton", "Go to ChatGPT", "", ""]], [["AXStaticText", "Skip setup?", "", ""]]]
    )

    def read_tree():
        tree = next(trees)
        app.ax_errors = []
        return tree

    app.read_tree = read_tree

    app.wait_gone("AXButton", "Go to ChatGPT", 60)


def test_wait_gone_keeps_waiting_while_the_traversal_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(macos_app.time, "sleep", lambda seconds: None)
    app = mac_app(RecordingSession(tmp_path / "artifacts"))
    # A busy app can return a partial tree with the button absent; that is not evidence the
    # dialog left until the traversal stops reporting AX errors.
    states = iter(
        [
            ([["AXApplication", "ChatGPT", "", ""]], ["children of AXApplication: cannotComplete"]),
            ([["AXButton", "Go to ChatGPT", "", ""]], []),
            ([["AXStaticText", "Skip setup?", "", ""]], []),
        ]
    )

    def read_tree():
        tree, app.ax_errors = next(states)
        return tree

    app.read_tree = read_tree

    app.wait_gone("AXButton", "Go to ChatGPT", 60)


def test_wait_gone_ignores_errors_beyond_the_web_content(tmp_path, monkeypatch):
    monkeypatch.setattr(macos_app.time, "sleep", lambda seconds: None)
    app = mac_app(RecordingSession(tmp_path / "artifacts"))
    # The app has window buttons whose children fetch fails on every traversal; once the web
    # content — where the journey targets live — was reached, those errors must not block the
    # absence conclusion.
    broken = ["children of AXButton at depth 2: AXError(rawValue: -25212)"]
    states = iter(
        [
            (
                [
                    ["AXApplication", "ChatGPT", "", ""],
                    ["AXWindow", "ChatGPT", "", ""],
                    ["AXWebArea", "ChatGPT", "", ""],
                    ["AXButton", "Go to ChatGPT", "", ""],
                ],
                broken,
            ),
            (
                [
                    ["AXApplication", "ChatGPT", "", ""],
                    ["AXWindow", "ChatGPT", "", ""],
                    ["AXWebArea", "ChatGPT", "", ""],
                    ["AXStaticText", "Skip setup?", "", ""],
                ],
                broken,
            ),
        ]
    )

    def read_tree():
        tree, app.ax_errors = next(states)
        return tree

    app.read_tree = read_tree

    app.wait_gone("AXButton", "Go to ChatGPT", 60)


def test_wait_gone_fails_with_the_ax_tree_when_the_dialog_stays(tmp_path, monkeypatch):
    clock = iter([0, 1000])
    monkeypatch.setattr(macos_app.time, "monotonic", lambda: next(clock))
    session = RecordingSession(tmp_path / "artifacts")
    app = mac_app(session)
    stuck = [["AXStaticText", "Skip setup?", "", ""], ["AXButton", "Go to ChatGPT", "", ""]]

    def read_tree():
        app.ax_errors = []
        return stuck

    app.read_tree = read_tree

    with pytest.raises(AssertionError, match="never dismissed AXButton 'Go to ChatGPT'"):
        app.wait_gone("AXButton", "Go to ChatGPT", 0)

    recorded = json.loads((session.artifacts / "chatgpt-tree.json").read_text())
    assert recorded == {
        "stuck_on": ["AXButton", "Go to ChatGPT"],
        "tree": stuck,
        "ax_errors": [],
    }


def test_app_environment_turns_off_the_app_sparkle_updater():
    session_env = {"HOME": "/session/home", "CODEX_HOME": "/session/home/.codex"}

    env = macos_app.app_environment(session_env)

    assert env["CODEX_SPARKLE_ENABLED"] == "false"
    assert env["HOME"] == "/session/home"
    assert "CODEX_SPARKLE_ENABLED" not in session_env


@pytest.mark.parametrize("state", ["dropped", "left", "stuck"])
def test_click_until_gone_repeats_a_dropped_click(tmp_path, monkeypatch, state):
    clock = itertools.count(0, 5)
    monkeypatch.setattr(macos_app.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(macos_app.time, "sleep", lambda seconds: None)
    session = RecordingSession(tmp_path / "artifacts")
    app = mac_app(session, client="macos-ax")
    dialog = [["AXButton", "Go to ChatGPT", "", ""]]
    clicks = []

    def run(*args, **kwargs):
        clicks.append(args)
        if state == "left" and len(clicks) == 2:
            raise AssertionError(
                "Fatal error: No AXButton labelled Go to ChatGPT (waited 2 seconds)"
            )

    def read_tree():
        app.ax_errors = []
        return dialog if state == "stuck" or len(clicks) < 2 else []

    session.run = run
    app.read_tree = read_tree

    if state == "stuck":
        with pytest.raises(AssertionError, match="never dismissed AXButton 'Go to ChatGPT'"):
            app.click_until_gone("AXButton", "Go to ChatGPT", 120)
        assert len(clicks) > 2
        assert json.loads((session.artifacts / "chatgpt-tree.json").read_text())["tree"] == dialog
    else:
        app.click_until_gone("AXButton", "Go to ChatGPT", 120)
        assert len(clicks) == 2
    assert clicks[0] == ("click", "1813", "AXButton", "Go to ChatGPT", "60")
