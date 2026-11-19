"""Launch a packaged Windows app in the session environment and drive it with UI Automation."""

import json
import shutil
import subprocess
import time
from pathlib import Path

from .harness import process_group_options, stop_process

UIA_CLIENT = Path(__file__).with_name("windows_uia.ps1")
# How long each screen gets to show the control a step waits for.
STEP_TIMEOUT = 60


class WindowsApp:
    def __init__(self, session, package: str, executable: str, name: str):
        self.session = session
        self.name = name
        self.log = session.home.parent / f"{name}.log"
        self.powershell = Path(shutil.which("powershell.exe") or "powershell.exe")
        found = subprocess.run(
            [str(self.powershell), "-NoProfile", "-Command"]
            + [
                f"Get-AppxPackage -Name {package} | Select-Object InstallLocation, Version | ConvertTo-Json"
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        ).stdout
        assert found.strip(), f"{package} is not installed"
        installed = json.loads(found)
        self.version = installed["Version"]
        self.executable = str(Path(installed["InstallLocation"]) / executable)

    def __enter__(self):
        self.session.record(f"{self.name}-app.json", {"version": self.version})
        with self.log.open("w") as log:
            # Chromium exposes its web content to UI Automation only when asked.
            self.proc = subprocess.Popen(
                [self.executable, "--force-renderer-accessibility"],
                env=self.session.env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                **process_group_options(),
            )
        return self

    def __exit__(self, *exc):
        try:
            self.uia("screenshot", str(self.session.artifacts / f"{self.name}.png"))
        except (AssertionError, subprocess.SubprocessError) as error:
            self.session.record(f"{self.name}-screenshot-error.json", {"error": str(error)})
        finally:
            stop_process(self.proc)
            log = self.session.redact(self.log.read_text(errors="replace"))
            self.session.record(f"{self.name}-log.json", {"log": log[-20000:]})

    def uia(self, command: str, *args: str) -> str:
        assert self.proc.poll() is None, f"{self.name} exited with {self.proc.returncode}"
        result = subprocess.run(
            [str(self.powershell), "-NoProfile", "-STA", "-File", str(UIA_CLIENT), command]
            + [str(self.proc.pid), *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            # A console window would take the foreground, and with it our keystrokes.
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        assert result.returncode == 0, f"UI Automation {command} failed: {result.stderr}"
        return result.stdout

    def read_tree(self) -> list[list[str]]:
        """Each element's control type, name and value."""
        try:
            return [line.split("\t") for line in self.uia("tree").splitlines()]
        except AssertionError as error:
            # The app has no window yet while it starts.
            if "has no window" not in str(error):
                raise
            return []

    def wait_for(
        self, control: str, name: str | None, timeout: float = STEP_TIMEOUT
    ) -> list[list[str]]:
        """Wait for a ``control`` element named ``name`` (any name if None)."""
        deadline = time.monotonic() + timeout
        while True:
            tree = self.read_tree()
            if any(row[0] == control and name in (None, *row[1:]) for row in tree):
                return tree
            if time.monotonic() > deadline:
                self.session.record(
                    f"{self.name}-tree.json", {"waiting_for": [control, name], "tree": tree}
                )
                raise AssertionError(f"{self.name} never showed {control} {name!r}")
            time.sleep(2)

    def click(self, control: str, name: str | None, timeout: float = STEP_TIMEOUT) -> None:
        """Activate the first enabled ``control`` element named ``name`` (any name if None)."""
        self.wait_for(control, name, timeout)
        self.uia("invoke", control, name or "")

    def type_line(self, text: str) -> None:
        """Type into the focused control with real keystrokes, then press Enter."""
        self.uia("type", text)
