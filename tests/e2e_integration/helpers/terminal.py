"""Small real-terminal driver. Unknown dialogs fail instead of being auto-approved."""

import contextlib
import os
import re
import signal
import time


class Terminal:
    def __init__(self, session, name, args, *, evidence=None):
        # Collection and unit evidence tests do not need the runner's PTY packages.
        import pexpect
        import pyte

        self.session, self.name, self.args = session, name, args
        self.evidence = evidence
        self.eof = False
        self.output, self.actions = [], []
        self.child = pexpect.spawn(
            str(session.binary),
            args,
            cwd=str(session.project),
            env=session.env,
            encoding="utf-8",
            codec_errors="replace",
            dimensions=(60, 160),
            timeout=1,
        )

        class Screen(pyte.Screen):
            def write_process_input(screen, data):
                self.child.send(data)

        self.screen = Screen(160, 60)
        self.stream = pyte.Stream(self.screen)

    @property
    def visible(self):
        return "\n".join(self.screen.display)

    def __enter__(self):
        return self

    def __exit__(self, *error):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.child.pid, signal.SIGTERM)
        self.child.close(force=True)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.child.pid, signal.SIGKILL)
        self.session.record(
            self.name,
            {
                "argv": [str(self.session.binary), *self.args],
                "actions": self.actions,
                "output": "".join(self.output),
                "screen": self.visible,
                "exit": self.child.exitstatus,
                "signal": self.child.signalstatus,
            },
        )
        if self.evidence:
            self.session.record(f"{self.name}-native", self.evidence.snapshot())

    def read(self):
        import pexpect

        try:
            data = self.child.read_nonblocking(65536, timeout=0.2)
        except pexpect.TIMEOUT:
            return
        except pexpect.EOF:
            self.eof = True
            return
        self.output.append(data)
        self.stream.feed(data)

    def send(self, text):
        self.actions.append({"keys": text, "screen": self.visible})
        self.child.send(text)

    def wait(self, predicate, description, timeout=30, stable=0.3):
        deadline, since = time.monotonic() + timeout, None
        while time.monotonic() < deadline:
            self.read()
            if predicate():
                since = since or time.monotonic()
                if self.eof or time.monotonic() - since >= stable:
                    return
            else:
                since = None
            assert not self.eof, self.session.redact(
                f"Exited before {description}:\n{self.visible}"
            )
        raise AssertionError(
            self.session.redact(f"Timed out awaiting {description}:\n{self.visible}")
        )

    def selected(self):
        return next(
            (line.strip() for line in self.visible.splitlines() if re.match(r"\s*[❯›>]", line)), ""
        )

    def choose(self, title, label):
        seen = set()
        while title in self.visible:
            before = self.selected()
            assert before and before not in seen, self.session.redact(
                f"Unknown menu: {self.visible}"
            )
            if label in before:
                self.send("\r")
                self.wait(lambda: title not in self.visible, f"confirmation of {label}")
                return
            seen.add(before)
            self.send("\x1b[B")
            self.wait(lambda before=before: self.selected() != before, "next choice")

    def boot(self, agent):
        handled = set()

        def ready():
            screen = self.visible
            assert "Select login method:" not in screen and not (
                "Sign in with ChatGPT" in screen and "Provide your own API key" in screen
            ), "Agent requested login instead of using the configured gateway"
            menus = [("Hooks need review", "Trust all and continue"), ("Update available", "Skip")]
            if self.session.project.name in screen:
                menus.extend([("Accessing workspace:", "Yes, I trust this folder")])
            for title, label in menus:
                if title in screen:
                    self.choose(title, label)
                    return False
            dialogs = {
                "theme": "Choose the text style" in screen and "Dark mode" in screen,
                "notes": "Security notes" in screen and "Enter to continue" in screen,
                "trust": self.session.project.name in screen
                and bool(
                    re.search(
                        r"[❯›>]\s*1[.)]\s+Yes, (?:I trust (?:this|the) folder|continue|proceed)",
                        screen,
                    )
                ),
            }
            for key, shown in dialogs.items():
                if shown:
                    if key not in handled:
                        self.send("\r")
                        handled.add(key)
                    return False
            title = "Claude Code" if agent == "claude" else "Codex"
            return bool(
                title in screen
                and "loading" not in screen.lower()
                and re.search(r"(?m)^\s*[❯›>]\s*(?!\d+[.)])", screen)
            )

        self.wait(ready, f"{agent} interactive prompt", timeout=150, stable=1)

    def submit(self, text):
        self.send(text)
        self.wait(
            lambda: "".join(text.split()) in "".join(self.visible.split()),
            "rendered input",
            stable=0.5,
        )
        self.send("\r")

    def task(self, evidence, task):
        def completed():
            screen = self.visible
            assert not re.search(
                r"unexpected status (?:400|401|403|404|422)\b|exceeded retry limit",
                screen,
                re.IGNORECASE,
            ), self.session.redact(screen)
            # Read is normally permitted in the trusted project. Never approve
            # arbitrary shell/tool requests to work around a failed file task.
            assert "Do you want to proceed?" not in screen, self.session.redact(
                "Unexpected permission request; inspect the actual command:\n" + screen
            )
            return evidence.completed(task) is not None

        self.wait(completed, "completed native file task", timeout=240)

    def finish(self, timeout=120):
        self.wait(lambda: self.eof, "normal process exit", timeout=timeout, stable=0)
        self.child.close(force=False)
        assert self.child.exitstatus == 0, self.session.redact(
            f"exit={self.child.exitstatus}, signal={self.child.signalstatus}\n{self.visible}"
        )

    def exit_normally(self):
        self.submit("/exit")
        self.finish(30)
