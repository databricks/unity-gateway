"""Launch a macOS app in the session environment and drive it as an Accessibility client."""

import plistlib
import subprocess
import time
from pathlib import Path

from .harness import process_group_options, stop_process

AX_CLIENT_SOURCE = Path(__file__).with_name("macos_ax.swift")
OSASCRIPT = Path("/usr/bin/osascript")


class MacApp:
    def __init__(self, session, bundle: Path, name: str):
        assert bundle.is_dir(), f"{bundle} is not installed"
        self.session = session
        self.name = name
        info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
        self.version = info["CFBundleShortVersionString"]
        self.executable = bundle / "Contents/MacOS" / info["CFBundleExecutable"]
        self.client = session.home.parent / "macos-ax"
        self.log = session.home.parent / f"{name}.log"

    def __enter__(self):
        self.session.run(
            "swiftc",
            "-O",
            "-o",
            str(self.client),
            str(AX_CLIENT_SOURCE),
            binary=Path("/usr/bin/xcrun"),
            timeout=600,
        )
        self.session.record(f"{self.name}-app.json", {"version": self.version})
        with self.log.open("w") as log:
            # The session environment gives the app the same HOME and CODEX_HOME as ug.
            self.proc = subprocess.Popen(
                [str(self.executable)],
                env=self.session.env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                **process_group_options(),
            )
        return self

    def __exit__(self, *exc):
        try:
            stop_process(self.proc)
        finally:
            log = self.session.redact(self.log.read_text(errors="replace"))
            self.session.record(f"{self.name}-log.json", {"log": log[-20000:]})

    def read_tree(self) -> list[list[str]]:
        """Each element's role, title, description and value."""
        assert self.proc.poll() is None, f"{self.name} exited with {self.proc.returncode}"
        result = subprocess.run(
            [str(self.client), "tree", str(self.proc.pid)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        return [line.split("\t") for line in result.stdout.splitlines()]

    def wait_for(self, role: str, label: str | None, timeout: float) -> list[list[str]]:
        """Wait for a ``role`` element labelled ``label`` (any label if None)."""
        deadline = time.monotonic() + timeout
        while True:
            tree = self.read_tree()
            if any(row[0] == role and label in (None, *row[1:]) for row in tree):
                return tree
            if time.monotonic() > deadline:
                self.session.record(
                    f"{self.name}-tree.json", {"waiting_for": [role, label], "tree": tree}
                )
                raise AssertionError(f"{self.name} never showed {role} {label!r}")
            time.sleep(2)

    def click(self, role: str, label: str | None, timeout: float = 60) -> None:
        """Click the first ``role`` element labelled ``label`` (any label if None)."""
        ready_tree = self.wait_for(role, label, timeout)
        try:
            self.session.run(
                "click", str(self.proc.pid), role, label or "", binary=self.client, timeout=60
            )
        except Exception as click_error:
            evidence = {"target": [role, label], "ready_tree": ready_tree}
            try:
                evidence["failure_tree"] = self.read_tree()
            except Exception as capture_error:
                evidence["capture_error"] = type(capture_error).__name__
            try:
                self.session.record(f"{self.name}-click-failure.json", evidence)
            except Exception as record_error:
                raise click_error from record_error
            raise

    def type_line(self, text: str) -> None:
        """Type into the focused control with real keystrokes, then press Return."""
        script = (
            "on run argv\n"
            'tell application "System Events"\n'
            "set frontmost of (first process whose unix id is (item 1 of argv as integer)) to true\n"
            "keystroke (item 2 of argv)\n"
            "key code 36\n"
            "end tell\n"
            "end run"
        )
        self.session.run("-e", script, str(self.proc.pid), text, binary=OSASCRIPT, timeout=60)
