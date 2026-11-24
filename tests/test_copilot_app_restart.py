"""``ug copilot-app-restart``: the detached helper ug's GitHub Copilot app plugin starts to restart
the app once no chat is running a turn."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from ucode.agents.desktop import copilot_app
from ucode.agents.desktop import copilot_app_restart as restart

TARGET = restart.Target("linux", 4242, "/usr/bin/github", Path("/home/u"))


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class FakeEvents:
    """The app's session events: ``script`` maps a time to the events sent then."""

    def __init__(self, clock: Clock, script: dict[float, list] | None = None, closes_at=None):
        self.clock = clock
        self.script = dict(script or {})
        self.closes_at = closes_at
        self.closed = False

    def next_message(self, timeout: float):
        if self.closes_at is not None and self.clock.t >= self.closes_at:
            raise copilot_app.CopilotAppNotRunning("closed")
        due = sorted(t for t in self.script if t <= self.clock.t)
        if due:
            session, kind = self.script.pop(due[0])
            return {"type": "session_event", "session_id": session, "event": {"type": kind}}
        self.clock.t += timeout
        return None

    def close(self) -> None:
        self.closed = True


class FakeSystem:
    def __init__(self, clock: Clock, *, exits=True, launches=(True,), osascript=True) -> None:
        self.clock = clock
        self.calls: list = []
        self.exits = exits
        self.stopped = False
        self.launches = list(launches)
        self.osascript = osascript

    def deps(self) -> dict:
        return {
            "alive": lambda pid: not (self.exits and self.stopped),
            "terminate": self.terminate,
            "run": self.run,
            "launch": self.launch,
            "sleep": self.clock.sleep,
            "clock": self.clock,
            "env": {"HOME": "/home/u", "DISPLAY": ":0", "GH_TOKEN": "secret", "COPILOT_X": "1"},
        }

    def terminate(self, pid):
        self.calls.append(("terminate", pid, self.clock.t))
        self.stopped = True

    def run(self, argv):
        self.calls.append(("run", argv))
        if argv[0] == "/usr/bin/osascript" and not self.osascript:
            return False
        self.stopped = True
        return True

    def launch(self, argv, env):
        self.calls.append(("launch", argv, dict(env)))
        return self.launches.pop(0) if self.launches else False


def _restart(script=None, *, closes_at=None, **system_kwargs):
    clock = Clock()
    events = FakeEvents(clock, script, closes_at)
    system = FakeSystem(clock, **system_kwargs)
    result = restart.restart_when_idle(TARGET, watch=lambda home: events, **system.deps())
    assert events.closed
    return result, system, clock


class TestTarget:
    def test_parses_the_extensions_target(self):
        text = json.dumps(
            {"platform": "win32", "pid": 7, "app": "C:\\a\\github.exe", "home": "/h", "log": "/l"}
        )

        assert restart.parse_target(text) == restart.Target(
            "win32", 7, "C:\\a\\github.exe", Path("/h"), Path("/l")
        )

    @pytest.mark.parametrize(
        "text",
        [
            "not json",
            "[]",
            json.dumps({"platform": "aix", "pid": 7, "app": "a", "home": "/h"}),
            json.dumps({"platform": "linux", "pid": True, "app": "a", "home": "/h"}),
            json.dumps({"platform": "linux", "pid": 1, "app": "a", "home": "/h"}),
            json.dumps({"platform": "linux", "pid": 7, "app": "", "home": "/h"}),
            json.dumps({"platform": "linux", "pid": 7, "app": "a"}),
        ],
    )
    def test_rejects_anything_else(self, text):
        assert restart.parse_target(text) is None


class TestWaitForIdle:
    def test_an_idle_app_restarts_after_the_quiet_period(self):
        result, system, _ = _restart()

        assert result == "relaunched"
        assert system.calls[0] == ("terminate", 4242, restart.QUIET_S)

    def test_a_turn_in_any_chat_delays_the_restart_until_that_chat_is_idle(self):
        result, system, _ = _restart(
            {5.0: ("ui-chat", "assistant.turn_start"), 90.0: ("ui-chat", "session.idle")}
        )

        assert result == "relaunched"
        killed_at = system.calls[0][2]
        assert 90.0 + restart.QUIET_S <= killed_at <= 90.0 + restart.QUIET_S + 1.0

    def test_a_chat_that_never_goes_idle_gives_up(self):
        result, system, clock = _restart({1.0: ("chat", "user.message")})

        assert result == "busy"
        assert clock.t == pytest.approx(restart.IDLE_TIMEOUT_S, abs=1.0)
        assert system.calls == []

    def test_events_that_keep_coming_hold_the_restart_off(self):
        script = {float(t): ("chat", "assistant.streaming_delta") for t in range(0, 400, 5)}

        result, system, _ = _restart(script)

        assert result == "busy"
        assert system.calls == []

    def test_an_app_that_closes_while_waiting_isnt_relaunched(self):
        result, system, _ = _restart({1.0: ("chat", "user.message")}, closes_at=3.0)

        assert result == "stopped"
        assert system.calls == []

    @pytest.mark.parametrize(
        ("error", "outcome"),
        [
            (copilot_app.CopilotAppNotRunning("closed"), "not_running"),
            (copilot_app.CopilotAppError("refused"), "no_session_events"),
        ],
    )
    def test_without_the_apps_session_events_it_gives_up(self, error, outcome):
        system = FakeSystem(Clock())

        def watch(home):
            raise error

        assert restart.restart_when_idle(TARGET, watch=watch, **system.deps()) == outcome
        assert system.calls == []


class TestRestartApp:
    def test_linux_terminates_then_relaunches_with_the_session_environment(self):
        system = FakeSystem(Clock())

        assert restart.restart_app(TARGET, **system.deps()) == "relaunched"

        assert system.calls[1] == (
            "launch",
            ["/usr/bin/github"],
            {"HOME": "/home/u", "DISPLAY": ":0"},
        )

    def test_a_failed_relaunch_is_retried_once(self):
        system = FakeSystem(Clock(), launches=(False, True))
        assert restart.restart_app(TARGET, **system.deps()) == "relaunched"

        system = FakeSystem(Clock(), launches=(False, False))
        assert restart.restart_app(TARGET, **system.deps()) == "relaunch_failed"
        assert [call[0] for call in system.calls] == ["terminate", "launch", "launch"]

    def test_an_app_that_doesnt_exit_is_left_running(self):
        clock = Clock()
        system = FakeSystem(clock, exits=False)

        assert restart.restart_app(TARGET, **system.deps()) == "still_running"

        assert [call[0] for call in system.calls] == ["terminate"]
        assert clock.t <= restart.EXIT_TIMEOUT_S + 1

    def test_macos_quits_the_app_then_opens_it_by_bundle_id(self):
        target = restart.Target("darwin", 4242, "com.github.githubapp", Path("/h"))
        system = FakeSystem(Clock())

        assert restart.restart_app(target, **system.deps()) == "relaunched"

        assert system.calls[0] == (
            "run",
            ["/usr/bin/osascript", "-e", 'quit app id "com.github.githubapp"'],
        )
        assert system.calls[1][1] == ["/usr/bin/open", "-b", "com.github.githubapp"]

    def test_macos_falls_back_to_sigterm_when_osascript_fails(self):
        target = restart.Target("darwin", 4242, "com.github.githubapp", Path("/h"))
        system = FakeSystem(Clock(), osascript=False)

        restart.restart_app(target, **system.deps())

        assert system.calls[1][0] == "terminate"

    def test_windows_closes_the_window_without_f_and_leaves_a_tray_app_running(self):
        target = restart.Target("win32", 4242, "C:\\GitHub\\github.exe", Path("/h"))
        system = FakeSystem(Clock(), exits=False)

        assert restart.restart_app(target, **system.deps()) == "still_running"

        assert system.calls == [("run", ["taskkill", "/PID", "4242"])]


def test_session_env_keeps_the_users_session_and_drops_the_runtimes():
    env = {
        "HOME": "/h",
        "PATH": "/bin",
        "LC_ALL": "C",
        "XDG_RUNTIME_DIR": "/run/u",
        "https_proxy": "http://p",
        "GH_TOKEN": "t",
        "COPILOT_SDK_AUTH_TOKEN": "x",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "https://w",
    }

    assert sorted(restart.session_env(env)) == [
        "HOME",
        "LC_ALL",
        "PATH",
        "XDG_RUNTIME_DIR",
        "https_proxy",
    ]


def test_main_logs_the_outcome_and_never_raises(tmp_path, monkeypatch):
    log = tmp_path / "app-restart.log"
    target = {"platform": "linux", "pid": 4242, "app": "/a", "home": str(tmp_path), "log": str(log)}
    outcomes = iter(["relaunched"])

    def boom(target):
        raise RuntimeError("x")

    monkeypatch.setattr(restart, "restart_when_idle", lambda target: next(outcomes))
    restart.main(json.dumps(target))
    monkeypatch.setattr(restart, "restart_when_idle", boom)
    restart.main(json.dumps(target))
    restart.main("not json")

    lines = log.read_text().splitlines()
    assert [line.split(" ", 1)[1] for line in lines] == ["relaunched", "failed: x"]


def test_without_a_running_app_the_real_watch_gives_up(tmp_path):
    target = restart.Target("linux", 4242, "/a", tmp_path)

    assert restart.restart_when_idle(target) == "not_running"


def test_the_cli_command_hands_the_target_to_the_helper(monkeypatch):
    from typer.testing import CliRunner

    from ucode.cli import app

    seen = []
    monkeypatch.setattr(restart, "main", seen.append)

    result = CliRunner().invoke(app, ["copilot-app-restart", '{"platform": "linux"}'])

    assert result.exit_code == 0, result.output
    assert seen == ['{"platform": "linux"}']


def test_the_real_watch_reads_the_apps_session_channels(tmp_path):
    from urllib.parse import parse_qs, urlparse

    from tests.test_copilot_app import FakeApp

    home = tmp_path / ".copilot"
    app = FakeApp(home)
    try:
        system = FakeSystem(Clock())
        deps = {**system.deps(), "clock": time.monotonic}
        target = restart.Target("linux", 4242, "/usr/bin/github", home)

        assert restart.restart_when_idle(target, quiet_s=0.3, **deps) == "relaunched"
    finally:
        app.stop()

    assert parse_qs(urlparse(app.paths[0]).query)["session_channels"] == ["true"]
