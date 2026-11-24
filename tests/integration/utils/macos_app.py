"""Launch a macOS app in the session environment and drive it as an Accessibility client."""

import plistlib
import subprocess
import time
from pathlib import Path

from .harness import process_group_options, stop_process

AX_CLIENT_SOURCE = Path(__file__).with_name("macos_ax.swift")
OSASCRIPT = Path("/usr/bin/osascript")


def app_environment(session_env: dict[str, str]) -> dict[str, str]:
    """The app's process environment: the session environment plus the updater-off switch.

    ChatGPT's Sparkle updater otherwise replaces the SHA-pinned build mid-job; the app reads
    this gate once at startup and leaves its updater uninitialized when it is "false"."""
    return {**session_env, "CODEX_SPARKLE_ENABLED": "false"}


def matches(row: list[str], role: str, label: str | None) -> bool:
    """True when an AX row is a ``role`` element whose title, description or value is ``label``.

    A ``None`` label matches any element of the role, mirroring the native client's matcher."""
    return row[0] == role and label in (None, *row[1:])


class MacApp:
    # The AX calls that failed in the last read_tree traversal, reported by the native client.
    ax_errors: list[str] = []

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
                env=app_environment(self.session.env),
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
        """Each element's role, title, description and value.

        The native client reports failed AX calls on stderr; they are kept on ``ax_errors`` so
        the waiters can tell a partial tree of a busy app from the whole screen."""
        assert self.proc.poll() is None, f"{self.name} exited with {self.proc.returncode}"
        result = subprocess.run(
            [str(self.client), "tree", str(self.proc.pid)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        self.ax_errors = [
            line.removeprefix("AX error: ")
            for line in result.stderr.splitlines()
            if line.startswith("AX error: ")
        ]
        return [line.split("\t") for line in result.stdout.splitlines()]

    def wait_for(self, role: str, label: str | None, timeout: float) -> list[list[str]]:
        """Wait for a ``role`` element labelled ``label`` (any label if None)."""
        deadline = time.monotonic() + timeout
        while True:
            tree = self.read_tree()
            if any(matches(row, role, label) for row in tree):
                return tree
            if time.monotonic() > deadline:
                self.session.record(
                    f"{self.name}-tree.json", {"waiting_for": [role, label], "tree": tree}
                )
                raise AssertionError(f"{self.name} never showed {role} {label!r}")
            time.sleep(2)

    def click(self, role: str, label: str | None, timeout: float = 60) -> None:
        """Click the first ``role`` element labelled ``label`` (any label if None).

        One native command activates the app, waits for the target and clicks that same
        element, retrying its own traversal until ``timeout``. A busy app can no longer race
        the wait with a second traversal that returns an empty tree."""
        self.session.run(
            "click",
            str(self.proc.pid),
            role,
            label or "",
            f"{timeout:g}",
            binary=self.client,
            timeout=int(timeout) + 30,
        )

    def wait_gone(self, role: str, label: str | None, timeout: float) -> None:
        """Wait for a ``role`` element labelled ``label`` to disappear.

        The click that should remove it is verified instead of assumed. A traversal that
        failed partway must not prove the element gone, so the wait continues while the native
        client reports AX errors, and a screen that never leaves the element fails with the
        stuck AX tree recorded as evidence."""
        deadline = time.monotonic() + timeout
        while True:
            tree = self.read_tree()
            if not self.ax_errors and not any(matches(row, role, label) for row in tree):
                return
            if time.monotonic() > deadline:
                tree = self.read_tree()
                self.session.record(
                    f"{self.name}-tree.json",
                    {"stuck_on": [role, label], "tree": tree, "ax_errors": self.ax_errors},
                )
                raise AssertionError(f"{self.name} never dismissed {role} {label!r}")
            time.sleep(2)

    def type_line(self, text: str, *, submit: bool = True) -> None:
        """Type with real keystrokes, optionally pressing Return."""
        enter = "key code 36\n" if submit else ""
        script = (
            "on run argv\n"
            'tell application "System Events"\n'
            "set frontmost of (first process whose unix id is (item 1 of argv as integer)) to true\n"
            "keystroke (item 2 of argv)\n"
            f"{enter}"
            "end tell\n"
            "end run"
        )
        self.session.run("-e", script, str(self.proc.pid), text, binary=OSASCRIPT, timeout=60)
