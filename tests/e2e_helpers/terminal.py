"""Neutral PTY rendering, keystrokes, deadlines, and cleanup.

The session supplies cwd, env, and a redacting record(name, payload) method.
No fixture configuration or workspace policy belongs here.
"""

import contextlib
import os
import re
import signal
import time

import pexpect
import pyte


class TerminalScreen(pyte.Screen):
    def __init__(self, columns, lines, send):
        super().__init__(columns, lines)
        self.send = send

    def write_process_input(self, data):
        # Real TUIs query cursor position/device attributes during startup.
        # pyte replies according to the terminal state it has actually rendered.
        self.send(data)


class TerminalProcess:
    def __init__(self, session, agent, command, name):
        self.session = session
        self.agent = agent
        self.name = name
        self.command = command
        env = {**session.env, "TERM": "xterm-256color"}
        self.child = pexpect.spawn(
            self.command[0],
            self.command[1:],
            cwd=str(session.cwd),
            env=env,
            encoding="utf-8",
            codec_errors="replace",
            dimensions=(60, 140),
            timeout=120,
        )
        self.screen = TerminalScreen(140, 60, self.child.send)
        self.stream = pyte.Stream(self.screen)
        self.output = []
        self.actions = []
        self.ended = False

    @property
    def visible(self):
        return "\n".join(line.rstrip() for line in self.screen.display)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        # pexpect creates a new session with a controlling terminal. Clean up
        # its process group even if the ug leader exited before its children.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.child.pid, signal.SIGTERM)
        self.child.close(force=True)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.child.pid, signal.SIGKILL)
        self.session.record(
            f"{self.name}.json",
            {
                "argv": self.command,
                "terminal": {"rows": 60, "columns": 140, "term": "xterm-256color"},
                "actions": self.actions,
                "transcript": "".join(self.output),
                "screen": self.visible,
                "exitstatus": self.child.exitstatus,
                "signalstatus": self.child.signalstatus,
                "normal_exit_observed": self.ended and self.child.exitstatus == 0,
                **self.evidence(),
            },
        )

    def evidence(self):
        """Suite-specific evidence, separate from terminal mechanics."""
        return {}

    def read(self):
        try:
            chunk = self.child.read_nonblocking(size=65536, timeout=0.2)
        except pexpect.TIMEOUT:
            return
        except pexpect.EOF:
            self.ended = True
            return
        self.output.append(chunk)
        self.stream.feed(chunk)

    def send(self, keys, reason):
        self.actions.append({"reason": reason, "keys": keys, "screen_before": self.visible})
        self.child.send(keys)

    def wait_for(self, predicate, description, timeout=30, stable_for=0.3):
        deadline = time.monotonic() + timeout
        since = None
        while time.monotonic() < deadline:
            self.read()
            if predicate(self.visible):
                # If the process has also finished (e.g. `ug configure` exits
                # right after the final pick), the screen is final and stable —
                # accept it instead of racing the exit.
                if self.ended:
                    return
                since = since or time.monotonic()
                if time.monotonic() - since >= stable_for:
                    return
            else:
                assert not self.ended, (
                    f"TUI exited while waiting for {description}:\n{self.visible}"
                )
                since = None
        raise AssertionError(f"TUI did not show {description} within {timeout}s:\n{self.visible}")

    def selected_line(self):
        return next(
            (line.strip() for line in self.visible.splitlines() if re.match(r"^\s*[›❯>]", line)),
            "",
        )

    def choose(self, prompt, label):
        """Navigate the visible menu with arrow keys; never write its saved state."""
        self.wait_for(lambda text: prompt in text and self.selected_line(), prompt, timeout=120)
        visited = set()
        for _ in range(100):
            current = self.selected_line()
            if label in current:
                self.send("\r", f"choose {label}")
                self.wait_for(
                    lambda text, before=current: (
                        prompt not in text or self.selected_line() != before
                    ),
                    f"confirmation of {label}",
                )
                return
            assert current not in visited, f"Menu does not offer {label}:\n{self.visible}"
            visited.add(current)
            self.send("\x1b[B", f"move towards {label}")
            self.wait_for(
                lambda text, before=current: self.selected_line() != before, "next menu option"
            )
        raise AssertionError(f"Could not select {label}")

    def submit(self, text):
        self.send(text, "type text before pressing Enter")
        compact = "".join(text.split())
        self.wait_for(
            lambda screen: compact in "".join(screen.split()), "typed input", stable_for=0.5
        )
        self.send("\r", "press Enter after the input has rendered")

    def finish(self, timeout=120):
        deadline = time.monotonic() + timeout
        while not self.ended and time.monotonic() < deadline:
            self.read()
        assert self.ended, f"Process did not exit within {timeout}s:\n{self.visible}"
        self.child.close(force=False)
        assert self.child.exitstatus == 0, (
            f"exit={self.child.exitstatus}, signal={self.child.signalstatus}:\n{self.visible}"
        )
