"""Launch a packaged Windows app in the session environment and drive it with UI Automation."""

import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import tomllib
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


def _sandbox_file_state(path: Path, *, config: bool) -> dict:
    try:
        stat = path.stat()
        text = path.read_text(encoding="utf-8-sig")
        data = tomllib.loads(text) if config else json.loads(text)
    except FileNotFoundError:
        return {"exists": False}
    except OSError as error:
        return {"exists": None, "error": type(error).__name__}
    except (UnicodeError, ValueError) as error:
        return {"exists": True, "error": type(error).__name__}
    result = {
        "exists": True,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(
            timespec="milliseconds"
        ),
    }
    if not isinstance(data, dict):
        result["error"] = "invalid-shape"
    elif config:
        windows = data.get("windows") if isinstance(data.get("windows"), dict) else {}
        mode = windows.get("sandbox")
        result["mode"] = mode if mode in (None, "elevated", "unelevated", "mxc") else "invalid"
        features = data.get("features") if isinstance(data.get("features"), dict) else {}
        result["features"] = {
            name: value
            for name in (
                "experimental_windows_sandbox",
                "elevated_windows_sandbox",
                "enable_experimental_windows_sandbox",
                "prefer_mxc",
            )
            if type(value := features.get(name)) is bool
        }
    else:
        version = data.get("version")
        result["version"] = version if type(version) is int else None
    return result


def _windows_sandbox_account(api, name: str) -> dict:
    import ctypes
    from ctypes import wintypes

    class UserInfo(ctypes.Structure):
        _fields_ = [
            ("name", wintypes.LPWSTR),
            ("password", wintypes.LPWSTR),
            ("password_age", wintypes.DWORD),
            ("privilege", wintypes.DWORD),
            ("home_dir", wintypes.LPWSTR),
            ("comment", wintypes.LPWSTR),
            ("flags", wintypes.DWORD),
            ("script_path", wintypes.LPWSTR),
        ]

    buffer = ctypes.c_void_p()
    status = api.NetUserGetInfo(None, name, 1, ctypes.byref(buffer))
    if status == 2221:
        return {"exists": False}
    if status != 0:
        return {"exists": None, "error_code": status}
    if not buffer.value:
        return {"exists": None, "error": "empty-response"}
    try:
        info = ctypes.cast(buffer, ctypes.POINTER(UserInfo)).contents
        return {"exists": True, "disabled": bool(info.flags & 0x0002)}
    finally:
        api.NetApiBufferFree(buffer)


def _sandbox_receipt_state(text: str, home: Path) -> dict:
    try:
        data = json.loads(text)
    except ValueError:
        return {"exists": True, "error": "invalid-json"}
    if not isinstance(data, dict):
        return {"exists": True, "error": "invalid-shape"}
    runtime = data.get("runtime")
    if not isinstance(runtime, dict):
        return {"exists": True, "runtime_present": False}
    configured_home = data.get("codex_home")
    entries = runtime.get("accounts")
    if not isinstance(entries, list):
        return {"exists": True, "error": "invalid-accounts"}
    accounts = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("account") not in ("offline", "online"):
            continue
        accounts[entry["account"]] = {
            "alias_present": isinstance(entry.get("alias_path"), str) and bool(entry["alias_path"]),
            "cleanup_pending": entry.get("cleanup_logon_pending") is True,
        }
    try:
        home_matches = isinstance(configured_home, str) and os.path.normcase(
            str(Path(configured_home).resolve())
        ) == os.path.normcase(str((home / ".codex").resolve()))
    except (OSError, RuntimeError, ValueError):
        home_matches = None
    return {
        "exists": True,
        "home_matches": home_matches,
        "ready_package_present": isinstance(runtime.get("ready_package"), str)
        and bool(runtime["ready_package"]),
        "retiring": runtime.get("retiring") is not None,
        "accounts": accounts,
    }


def _windows_machine_provisioning(home: Path) -> dict:
    if os.name != "nt":
        return {"status": "unsupported"}
    import ctypes
    import winreg
    from ctypes import wintypes

    try:
        api = ctypes.WinDLL("netapi32", use_last_error=True)
        api.NetUserGetInfo.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        api.NetUserGetInfo.restype = wintypes.DWORD
        api.NetApiBufferFree.argtypes = [ctypes.c_void_p]
        api.NetApiBufferFree.restype = wintypes.DWORD
        accounts = {
            "offline": _windows_sandbox_account(api, "CodexSandboxOffline"),
            "online": _windows_sandbox_account(api, "CodexSandboxOnline"),
        }
    except OSError as error:
        accounts = {"error": type(error).__name__}
    base = r"SOFTWARE\OpenAI\Codex\WindowsSandboxService"
    registration = {}
    for label, key in (("service", base), ("registered_core", base + r"\RegisteredCore")):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as handle:
                value, value_type = winreg.QueryValueEx(handle, "ProvisionedInstallation")
            if value_type != winreg.REG_SZ or not isinstance(value, str):
                registration[label] = {"exists": True, "error": "invalid-type"}
            else:
                registration[label] = _sandbox_receipt_state(value, home)
        except FileNotFoundError:
            registration[label] = {"exists": False}
        except OSError as error:
            registration[label] = {"exists": None, "error": type(error).__name__}
    return {"accounts": accounts, "registration": registration}


def windows_sandbox_provisioning_snapshot(home: Path) -> dict:
    codex = home / ".codex"
    return {
        "observed_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "config": _sandbox_file_state(codex / "config.toml", config=True),
        "marker": _sandbox_file_state(codex / ".sandbox" / "setup_marker.json", config=False),
        "users_file": _sandbox_file_state(
            codex / ".sandbox-secrets" / "sandbox_users.json", config=False
        ),
        "machine": _windows_machine_provisioning(home),
    }


def record_windows_sandbox_selection(session) -> None:
    pattern = re.compile(
        r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} codex\.exe\] "
        r"(?:Windows sandbox selection: (?:disabled|unelevated|elevated|mxc)|"
        r"MXC availability: (?:true|false))"
    )
    lines = []
    try:
        for path in sorted((session.home / ".codex" / ".sandbox").glob("sandbox.*.log")):
            lines.extend(
                line
                for line in path.read_text(encoding="utf-8").splitlines()
                if pattern.fullmatch(line)
            )
    except (OSError, UnicodeError) as error:
        session.record(
            "windows-sandbox-selection.json", {"lines": lines, "error": type(error).__name__}
        )
        return
    session.record("windows-sandbox-selection.json", {"lines": lines})


def record_windows_proxy_diagnostics(session) -> None:
    exceptions = {
        "BrokenPipeError",
        "ConnectionError",
        "ConnectionResetError",
        "OSError",
        "RuntimeError",
        "TimeoutError",
        "ValueError",
        "httpcore.ConnectError",
        "httpcore.ConnectTimeout",
        "httpcore2.ConnectError",
        "httpcore2.ConnectTimeout",
        "httpx.ConnectError",
        "httpx.ConnectTimeout",
        "httpx.ReadTimeout",
        "httpx2.ConnectError",
        "httpx2.ConnectTimeout",
        "httpx2.ReadTimeout",
    }
    frame = re.compile(
        r'File "[^"]*[/\\](ucode|mcp|httpx2?|httpcore2?|anyio)[/\\]'
        r'(?:[^"]*[/\\])?([A-Za-z0-9_]+\.py)", line (\d+)'
    )
    events = []
    paths = sorted((session.home / ".ucode" / "mcp-proxy").glob("*/backend.log"))
    try:
        for path in paths:
            for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.lstrip(" |+")
                if line.startswith("Traceback (most recent call last):"):
                    events.append({"kind": "traceback"})
                elif match := frame.match(line):
                    events.append(
                        {
                            "kind": "frame",
                            "package": match[1],
                            "file": match[2],
                            "line": int(match[3]),
                        }
                    )
                elif (name := line.partition(":")[0]) in exceptions:
                    events.append({"kind": "exception", "type": name})
                elif match := re.search(r"\bHTTP ([1-5]\d{2})\b", line):
                    events.append({"kind": "http_status", "status": int(match[1])})
                elif match := re.search(r"\bexited with code (\d+)\b", line):
                    events.append({"kind": "exit_code", "code": int(match[1])})
    except OSError as error:
        session.record(
            "windows-mcp-proxy-diagnostics.json",
            {"log_count": len(paths), "events": events, "error": type(error).__name__},
        )
        return
    session.record(
        "windows-mcp-proxy-diagnostics.json", {"log_count": len(paths), "events": events}
    )


def _redact_app_log(session, text: str) -> str:
    secret = os.environ.get("DATABRICKS_CLIENT_SECRET", "").strip()
    if secret:
        text = text.replace(secret, "<redacted>")
    return session.redact(text)


def _windows_process_start(pid: int) -> str:
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return datetime.fromtimestamp(ticks / 10_000_000 - 11_644_473_600, UTC).isoformat(
            timespec="milliseconds"
        )
    finally:
        kernel.CloseHandle(handle)


def _capture_app_log(
    stream, path: Path, events: list[dict], condition: threading.Condition
) -> None:
    with path.open("w", encoding="utf-8") as log:
        for line in stream:
            observed_at = datetime.now(UTC).isoformat(timespec="milliseconds")
            log.write(f"{observed_at} {line}")
            log.flush()
            event = None
            if "stdio_transport_spawned" in line and (match := re.search(r"\bpid=(\d+)", line)):
                pid = int(match.group(1))
                event = {"event": "stdio-transport-spawn", "observed_at": observed_at, "pid": pid}
                try:
                    event["process_started_at"] = _windows_process_start(pid)
                except OSError as error:
                    event["process_start_error"] = str(error)
            elif match := re.search(
                r"method=windowsSandbox/(readiness|setupStart|setupComplete)\b", line
            ):
                error_code = re.search(r"\berrorCode=(null|-?\d+|[A-Z][A-Z0-9_]*)\b", line)
                event = {
                    "event": match.group(1),
                    "observed_at": observed_at,
                    "error_code": error_code.group(1) if error_code else None,
                }
            elif "app_server_connection.state_changed" in line and re.search(
                r"\bhostId=local(?:\s|$)", line
            ):
                if match := re.search(r"\bcause=(stop_process|start_process)\b", line):
                    event = {"event": match.group(1), "observed_at": observed_at}
                elif re.search(r"\binitialized=true(?:\s|$)", line) and re.search(
                    r"\bnext=connected(?:\s|$)", line
                ):
                    event = {"event": "connected", "observed_at": observed_at}
            elif (
                "response_routed" in line
                and re.search(r"\bmethod=model/list(?:\s|$)", line)
                and re.search(r"\berrorCode=null(?:\s|$)", line)
            ):
                event = {"event": "model/list", "observed_at": observed_at}
            elif "Hosted AppServerManager identity is not ready" in line:
                event = {"event": "identity-not-ready", "observed_at": observed_at}
            if event:
                with condition:
                    events.append(event)
                    condition.notify_all()


def _restart_experiment_summary(timings: list[dict], events: list[dict]) -> dict:
    phases = {
        entry.get("phase"): entry.get("started_at") for entry in timings if entry.get("phase")
    }
    gate = phases.get("restart-experiment-gate-open")
    first = phases.get("first-interaction-start")
    gate_time = datetime.fromisoformat(gate) if gate else None
    first_time = datetime.fromisoformat(first) if first else None
    errors = []
    if gate_time:
        for event in events:
            if event.get("event") != "identity-not-ready":
                continue
            observed = datetime.fromisoformat(event["observed_at"])
            if observed >= gate_time:
                errors.append(
                    {
                        "observed_at": event["observed_at"],
                        "relative_to_first_interaction_ms": (
                            (observed - first_time).total_seconds() * 1000 if first_time else None
                        ),
                    }
                )
    return {
        "gate_opened": gate_time is not None,
        "gate_opened_at": gate,
        "first_interaction_at": first,
        "identity_not_ready_after_gate": errors,
    }


class WindowsUIATimeout(AssertionError, subprocess.TimeoutExpired):
    def __init__(self, timeout: float, stderr: str):
        subprocess.TimeoutExpired.__init__(self, "UI Automation helper", timeout, stderr=stderr)

    def __str__(self) -> str:
        return "UI Automation helper did not respond"


class WindowsUIAProcess:
    def __init__(self, argv: list[str]):
        started = time.monotonic()
        self.stderr = tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace")
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
        self.last_diagnostics: dict = {}

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
            try:
                self.stderr.flush()
                self.stderr.seek(0)
                diagnostic = self.stderr.read()[-2000:]
                self.stderr.seek(0, os.SEEK_END)
            except OSError:
                diagnostic = ""
            raise WindowsUIATimeout(timeout, diagnostic) from error
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
        self.last_diagnostics = {}
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
        try:
            self.proc.stdin.write(json.dumps({"command": command, "args": args}) + "\n")
            self.proc.stdin.flush()
        except OSError as error:
            self.broken = True
            raise AssertionError("UI Automation helper input closed") from error
        timeout = float(args[2]) + 5 if command in ("click", "wait") else 60
        response = self._receive(timeout)
        self.last_diagnostics = response.get("diagnostics", {})
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
                "target_snapshot_ms",
                "picker_new_window_count",
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
        try:
            self.proc.stdin.close()
        except OSError:
            self.broken = True
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
        self.setup_events: list[dict] = []
        self.setup_condition = threading.Condition()
        self.baseline_sampled = False
        self.version, location = _installed_package(package)
        self.executable = str(Path(location) / executable)

    def __enter__(self):
        self.session.record(f"{self.name}-app.json", {"version": self.version})
        self.session.record(
            "windows-sandbox-prelaunch.json",
            windows_sandbox_provisioning_snapshot(self.session.home),
        )
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
            target=_capture_app_log,
            args=(self.proc.stdout, self.log, self.setup_events, self.setup_condition),
            daemon=True,
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
            self.session.record(f"{self.name}-setup-events.json", self.setup_events)
            self.session.record(
                f"{self.name}-restart-experiment.json",
                _restart_experiment_summary(self.timings, self.setup_events),
            )
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
        started_at = datetime.now(UTC).isoformat(timespec="milliseconds")
        output = ""
        try:
            output = self.uia_process.request(command, *args)
            return output
        except subprocess.TimeoutExpired as error:
            diagnostic = error.stderr or ""
            if isinstance(diagnostic, bytes):
                diagnostic = diagnostic.decode("utf-8", errors="replace")
            try:
                self.session.record(
                    f"{self.name}-uia-{command}-timeout.json",
                    {"command": command, "stderr": self.session.redact(diagnostic[-2000:])},
                )
            except OSError as evidence_error:
                error.add_note(
                    f"Could not record UI Automation timeout evidence: {type(evidence_error).__name__}"
                )
            raise
        finally:
            timing = {
                "action": command,
                "started_at": started_at,
                "ended_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "seconds": time.monotonic() - started,
            }
            timing.update(self.uia_process.last_metrics)
            if self.uia_process.last_diagnostics:
                timing["diagnostics"] = self.uia_process.last_diagnostics
            if command in ("click", "wait") and output.isdigit():
                timing["polls"] = int(output)
            self.timings.append(timing)

    def mark_setup_phase(self, phase: str) -> None:
        self.timings.append(
            {
                "action": "setup-phase",
                "phase": phase,
                "started_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
            }
        )

    def setup_event_checkpoint(self) -> int:
        with self.setup_condition:
            return len(self.setup_events)

    def wait_for_setup_restart(self, checkpoint: int, deadline: float) -> None:
        phase = 0
        with self.setup_condition:
            while True:
                for event in self.setup_events[checkpoint:]:
                    kind = event.get("event")
                    if kind == "setupStart" and event.get("error_code") == "null":
                        phase = 1
                    elif kind == "stop_process" and phase:
                        phase = 1
                    elif kind == "start_process" and phase:
                        phase = 2
                    elif kind == "connected" and phase == 2:
                        phase = 3
                    elif kind == "model/list" and phase == 3:
                        self.mark_setup_phase("restart-experiment-gate-open")
                        return
                checkpoint = len(self.setup_events)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(
                        "The post-setup restart did not reach connected model discovery"
                    )
                self.setup_condition.wait(remaining)

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

    def _control_action(
        self, action: str, control: str, name: str | None, timeout: float, *extra: str
    ) -> None:
        try:
            self.uia(action, control, name or "", f"{timeout:g}", *extra)
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

    def click(
        self,
        control: str,
        name: str | None,
        timeout: float = STEP_TIMEOUT,
        *,
        trace_picker: bool = False,
    ) -> None:
        """Activate the first enabled ``control`` element named ``name`` (any name if None)."""
        self._control_action("click", control, name, timeout, *(["picker"] if trace_picker else []))

    def type_line(self, text: str, *, submit: bool = True) -> None:
        """Type with real keystrokes, optionally pressing Enter."""
        self.uia("type", text, "enter" if submit else "")
