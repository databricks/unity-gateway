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

        The click that should remove it is verified instead of assumed. A traversal that never
        reached the app's web content — where every journey target lives — cannot prove the
        element gone, so such a failed traversal keeps the wait going; AX errors on elements
        beyond the web content do not, because the app has window buttons whose children fetch
        fails on every single traversal. A screen that never leaves the element fails with the
        stuck AX tree recorded as evidence."""
        deadline = time.monotonic() + timeout
        while True:
            tree = self.read_tree()
            settled = not self.ax_errors or any(row[0] == "AXWebArea" for row in tree)
            if settled and not any(matches(row, role, label) for row in tree):
                return
            if time.monotonic() > deadline:
                tree = self.read_tree()
                self.session.record(
                    f"{self.name}-tree.json",
                    {"stuck_on": [role, label], "tree": tree, "ax_errors": self.ax_errors},
                )
                raise AssertionError(f"{self.name} never dismissed {role} {label!r}")
            time.sleep(2)

    def click_until_gone(self, role: str, label: str | None, timeout: float) -> None:
        """Click a ``role`` element labelled ``label``, again while it stays on screen.

        The app can drop a click the native command reported: run 45532827343 clicked
        Go to ChatGPT once and the Skip setup? dialog was still up 120 seconds later. The click
        is repeated every 10 seconds until the element leaves; one that is still there at
        ``timeout`` fails with the stuck AX tree recorded."""
        deadline = time.monotonic() + timeout
        self.click(role, label)
        while True:
            try:
                self.wait_gone(role, label, 10)
                return
            except AssertionError as error:
                stuck = str(error) == f"{self.name} never dismissed {role} {label!r}"
                if not stuck or time.monotonic() > deadline:
                    raise
            try:
                self.click(role, label, timeout=2)
            except AssertionError as error:
                # The element can leave between the last traversal and this click.
                if f"Fatal error: No {role} labelled {label or ''}" not in str(error):
                    raise

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
