"""Launch a packaged Windows app in the session environment and drive it with UI Automation."""

import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from .harness import process_group_options, stop_process

UIA_CLIENT = Path(__file__).with_name("windows_uia.ps1")
# How long each screen gets to show the control a step waits for.
STEP_TIMEOUT = 60


# The per-user AppX repository that Get-AppxPackage reads. Querying it through that cmdlet took
# over 60 s on hosted Windows runners in 20 of 141 runs; the registry read is immediate.
APPX_PACKAGES_KEY = (
    r"Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion"
    r"\AppModel\Repository\Packages"
)


def _installed_package(package: str) -> tuple[str, str]:
    """Return the newest installed version of ``package`` and its install folder."""
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPX_PACKAGES_KEY) as packages:
        names = [
            winreg.EnumKey(packages, index) for index in range(winreg.QueryInfoKey(packages)[0])
        ]
        # Full names are <name>_<version>_<architecture>_<resource id>_<publisher id>. Resource and
        # split packages share the name and version but have a resource id and no executable.
        matches = [
            name for name in names if name.split("_")[0] == package and name.split("_")[3:4] == [""]
        ]
        assert matches, f"{package} is not installed"
        full_name = max(
            matches, key=lambda name: tuple(int(part) for part in name.split("_")[1].split("."))
        )
        with winreg.OpenKey(packages, full_name) as key:
            location, _ = winreg.QueryValueEx(key, "PackageRootFolder")
    return full_name.split("_")[1], location


def _redact_app_log(session, text: str) -> str:
    secret = os.environ.get("DATABRICKS_CLIENT_SECRET", "").strip()
    if secret:
        text = text.replace(secret, "<redacted>")
    return session.redact(text)


def _capture_app_log(stream, path: Path) -> None:
    with path.open("w", encoding="utf-8") as log:
        for line in stream:
            log.write(f"{datetime.now(UTC).isoformat(timespec='milliseconds')} {line}")
            log.flush()


class WindowsUIAProcess:
    def __init__(self, argv: list[str]):
        started = time.monotonic()
        self.stderr = tempfile.TemporaryFile(mode="w+t", encoding="utf-8")
        try:
            self.proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self.stderr,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
            )
        except OSError:
            self.stderr.close()
            raise
        self.responses = queue.Queue()
        self.broken = False
        self.ready = False
        self.started = started
        self.init_metrics: dict[str, float] = {}
        self.last_metrics: dict[str, float] = {}

        def read_responses():
            for line in self.proc.stdout:
                self.responses.put(line)
            self.responses.put(None)

        self.reader = threading.Thread(target=read_responses, daemon=True)
        self.reader.start()

    def _receive(self, timeout: float) -> dict:
        try:
            line = self.responses.get(timeout=timeout)
        except queue.Empty as error:
            self.broken = True
            raise AssertionError("UI Automation helper did not respond") from error
        if line is None:
            self.broken = True
            raise AssertionError("UI Automation helper exited without a response")
        try:
            return json.loads(line)
        except ValueError as error:
            self.broken = True
            raise AssertionError("UI Automation helper returned invalid JSON") from error

    def request(self, command: str, *args: str) -> str:
        self.last_metrics = {}
        assert not self.broken, "UI Automation helper is unavailable"
        assert self.proc.poll() is None, "UI Automation helper exited"
        if not self.ready:
            ready = self._receive(60)
            if ready.get("event") != "ready":
                self.broken = True
                raise AssertionError("UI Automation helper did not send a ready event")
            self.init_metrics = ready.get("metrics", {})
            self.init_metrics["process_ready_ms"] = (time.monotonic() - self.started) * 1000
            self.ready = True
        self.proc.stdin.write(json.dumps({"command": command, "args": args}) + "\n")
        self.proc.stdin.flush()
        timeout = float(args[2]) + 5 if command in ("click", "wait") else 60
        response = self._receive(timeout)
        metrics = {**self.init_metrics, **response.get("metrics", {})}
        self.last_metrics = {
            key: value
            for key, value in metrics.items()
            if key
            in {
                "init_ms",
                "process_to_script_ms",
                "module_ms",
                "uia_assembly_ms",
                "forms_assembly_ms",
                "root_ms",
                "process_ready_ms",
                "action_ms",
                "wait_ms",
                "click_ms",
                "composer_focus_before_wait",
                "composer_focus_at_first_key",
                "composer_focus_wait_ms",
                "click_to_first_key_ms",
                "polls",
            }
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        }
        assert response["ok"], f"UI Automation {command} failed: {response['error']}"
        return response["output"]

    def close(self) -> None:
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.reader.join(timeout=5)
        self.proc.stdout.close()
        self.stderr.seek(0)
        self.stderr_output = self.stderr.read()
        self.stderr.close()


class WindowsApp:
    def __init__(self, session, package: str, executable: str, name: str):
        self.session = session
        self.name = name
        self.log = session.home.parent / f"{name}.log"
        self.powershell = Path(shutil.which("powershell.exe") or "powershell.exe")
        self.timings: list[dict] = []
        self.baseline_sampled = False
        self.version, location = _installed_package(package)
        self.executable = str(Path(location) / executable)

    def __enter__(self):
        self.session.record(f"{self.name}-app.json", {"version": self.version})
        started = time.monotonic()
        # Chromium exposes its web content to UI Automation only when asked.
        self.proc = subprocess.Popen(
            [self.executable, "--force-renderer-accessibility"],
            env=self.session.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            **process_group_options(),
        )
        self.reader = threading.Thread(
            target=_capture_app_log, args=(self.proc.stdout, self.log), daemon=True
        )
        self.reader.start()
        self.timings.append({"action": "app_spawn", "seconds": time.monotonic() - started})
        started = time.monotonic()
        try:
            self.uia_process = WindowsUIAProcess(
                [
                    str(self.powershell),
                    "-NoProfile",
                    "-NonInteractive",
                    "-STA",
                    "-File",
                    str(UIA_CLIENT),
                    "session",
                    str(self.proc.pid),
                ]
            )
        except OSError:
            stop_process(self.proc)
            self.reader.join(timeout=5)
            raise
        self.timings.append({"action": "uia_spawn", "seconds": time.monotonic() - started})
        return self

    def __exit__(self, *exc):
        try:
            self.uia("screenshot", str(self.session.artifacts / f"{self.name}.png"))
        except (AssertionError, subprocess.SubprocessError) as error:
            self.session.record(f"{self.name}-screenshot-error.json", {"error": str(error)})
        finally:
            try:
                self.uia_process.close()
            finally:
                stop_process(self.proc)
            self.reader.join(timeout=5)
            self.session.record(
                f"{self.name}-uia-stderr.json",
                {"stderr": _redact_app_log(self.session, self.uia_process.stderr_output)},
            )
            self.session.record(f"{self.name}-uia-timings.json", self.timings)
            log = _redact_app_log(self.session, self.log.read_text(errors="replace"))
            self.session.record(
                f"{self.name}-log.json",
                {
                    "log": log,
                    "mcp_and_setup": "\n".join(
                        line
                        for line in log.splitlines()
                        if "mcp" in line.lower() or "sandbox" in line.lower()
                    )[-20000:],
                },
            )
            assert not self.reader.is_alive(), "Desktop log capture did not finish"
            self.proc.stdout.close()

    def uia(self, command: str, *args: str) -> str:
        assert self.proc.poll() is None, f"{self.name} exited with {self.proc.returncode}"
        started = time.monotonic()
        output = ""
        try:
            output = self.uia_process.request(command, *args)
            return output
        finally:
            timing = {"action": command, "seconds": time.monotonic() - started}
            timing.update(self.uia_process.last_metrics)
            if command in ("click", "wait") and output.isdigit():
                timing["polls"] = int(output)
            self.timings.append(timing)

    def read_tree(self) -> list[list[str]]:
        """Each element's control type, name and value."""
        try:
            output = self.uia("tree")
            if (
                output
                and os.environ.get("UG_UIA_BASELINE_SAMPLE") == "1"
                and not self.baseline_sampled
            ):
                self.baseline_sampled = True
                started = time.monotonic()
                try:
                    result = subprocess.run(
                        [
                            str(self.powershell),
                            "-NoProfile",
                            "-NonInteractive",
                            "-STA",
                            "-File",
                            str(UIA_CLIENT),
                            "tree",
                            str(self.proc.pid),
                        ],
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        timeout=60,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    code = result.returncode
                except (OSError, subprocess.TimeoutExpired):
                    code = "unavailable"
                self.timings.append(
                    {
                        "action": "legacy_tree_sample",
                        "seconds": time.monotonic() - started,
                        "returncode": code,
                    }
                )
            return [line.split("\t") for line in output.splitlines()]
        except AssertionError as error:
            # The app has no window yet while it starts.
            if "has no window" not in str(error):
                raise
            return []

    def _control_action(self, action: str, control: str, name: str | None, timeout: float) -> None:
        try:
            self.uia(action, control, name or "", f"{timeout:g}")
        except AssertionError as error:
            try:
                self.session.record(
                    f"{self.name}-tree.json",
                    {"waiting_for": [control, name], "tree": self.read_tree()},
                )
            except (AssertionError, OSError) as evidence_error:
                error.add_note(f"Could not record the accessibility tree: {evidence_error}")
            raise

    def wait_for(
        self, control: str, name: str | None, timeout: float = STEP_TIMEOUT
    ) -> list[list[str]]:
        """Wait for a ``control`` element named ``name`` (any name if None)."""
        self._control_action("wait", control, name, timeout)
        tree = self.read_tree()
        if not any(row[0] == control and name in (None, *row[1:]) for row in tree):
            self.session.record(
                f"{self.name}-tree.json", {"waiting_for": [control, name], "tree": tree}
            )
            raise AssertionError(f"{self.name} target disappeared before tree capture")
        return tree

    def click(self, control: str, name: str | None, timeout: float = STEP_TIMEOUT) -> None:
        """Activate the first enabled ``control`` element named ``name`` (any name if None)."""
        self._control_action("click", control, name, timeout)

    def type_line(self, text: str, *, submit: bool = True) -> None:
        """Type with real keystrokes, optionally pressing Enter."""
        self.uia("type", text, "enter" if submit else "")
