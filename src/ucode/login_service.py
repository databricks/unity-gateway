"""ug's login service: the always-on loopback proxy that desktop agents reach.

One per user, installed as a systemd user unit, a macOS LaunchAgent or a Windows scheduled task.
It runs ``ug otel-proxy`` and keeps every listener's port in its own arguments.
"""

from __future__ import annotations

import os
import plistlib
import re
import socket
import sys
import threading
from pathlib import Path
from subprocess import CalledProcessError, list2cmdline
from xml.sax.saxutils import escape

from ucode.config_io import APP_DIR, write_text_file
from ucode.constants import AGENT_CODEX, LOOPBACK_HOST
from ucode.custom_oauth import CUSTOM_OAUTH_CLI_ENV_VAR, launch_token
from ucode.databricks import apply_pat_environment, ug_binary
from ucode.managed_files import OS, current_os
from ucode.os_compatibility import subprocess_cross_os
from ucode.state import load_full_state
from ucode.ui import print_err
from ucode.workspace import pin, workspace_id_args
from ucode.workspace_key import workspace_key

SERVICE_NAME = "ug-codex-otel.service"
UNIT_MARKER = "# Managed by unity-gateway; do not edit."
# Added to the marker while the GitHub Copilot app needs the service, so Codex keeps it.
COPILOT_APP_NOTE = "Used by the GitHub Copilot app."
# The macOS LaunchAgent label and the Windows scheduled task name.
SERVICE_LABEL = "com.databricks.ug-codex-otel"


def service_path() -> Path:
    if current_os() == OS.MACOS:
        return Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"
    if current_os() == OS.WINDOWS:
        # Task Scheduler keeps its own copy; this one records ownership, the port, and changes.
        return APP_DIR / f"{SERVICE_LABEL}.xml"
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config_home.expanduser() / "systemd" / "user" / SERVICE_NAME


def _run_systemctl(args: list[str]) -> None:
    try:
        subprocess_cross_os.run(
            ["systemctl", "--user", *args], check=True, capture_output=True, text=True
        )
    except (OSError, CalledProcessError) as exc:
        detail = exc.stderr if isinstance(exc, CalledProcessError) else str(exc)
        raise RuntimeError(f"systemctl --user {' '.join(args)} failed: {detail}") from exc


def _run_service_command(argv: list[str], *, check: bool = True) -> bool:
    """Run a service manager command; without ``check``, a failed exit returns False."""
    try:
        subprocess_cross_os.run(argv, check=True, capture_output=True, text=True)
    except CalledProcessError as exc:
        if not check:
            return False
        raise RuntimeError(f"{' '.join(argv)} failed: {exc.stderr}") from exc
    except OSError as exc:
        raise RuntimeError(f"{' '.join(argv)} failed: {exc}") from exc
    return True


def _service_env() -> dict[str, str]:
    """The configuring shell's settings that the proxy's token provider needs after login."""
    env = {}
    for name in (
        "PATH",
        "DATABRICKS_CONFIG_FILE",
        "DATABRICKS_AUTH_STORAGE",
        CUSTOM_OAUTH_CLI_ENV_VAR,
    ):
        value = os.environ.get(name)
        if name == "DATABRICKS_CONFIG_FILE" and value:
            value = str(Path(value).expanduser().absolute())
        if value:
            env[name] = value
    return env


def _ensure_launch_agent(path: Path, argv: list[str], marker: str) -> None:
    plist = plistlib.dumps(
        {
            "Label": SERVICE_LABEL,
            "ProgramArguments": argv,
            "EnvironmentVariables": _service_env(),
            "RunAtLoad": True,
            # Restart only after a failure, like the systemd unit's Restart=on-failure.
            "KeepAlive": {"SuccessfulExit": False},
        }
    ).decode("utf-8")
    text = plist.replace("<plist", f"<!-- {marker} -->\n<plist", 1)
    changed = not path.is_file() or path.read_text(encoding="utf-8") != text
    if changed:
        write_text_file(path, text)
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{SERVICE_LABEL}"
    loaded = _run_service_command(["launchctl", "print", target], check=False)
    if loaded and not changed:
        return  # KeepAlive keeps a loaded agent running.
    if loaded:
        # A loaded agent keeps its old definition; only a reload reads the new plist.
        _run_service_command(["launchctl", "bootout", target])
    _run_service_command(["launchctl", "bootstrap", domain, str(path)])


def _ensure_scheduled_task(path: Path, argv: list[str], marker: str) -> None:
    # A non-admin may only register a logon trigger for their own account.
    user = "\\".join(filter(None, (os.environ.get("USERDOMAIN"), os.environ.get("USERNAME"))))
    text = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>{marker}</Description></RegistrationInfo>
  <Triggers><LogonTrigger><UserId>{escape(user)}</UserId></LogonTrigger></Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(argv[0])}</Command>
      <Arguments>{escape(list2cmdline(argv[1:]))}</Arguments>
    </Exec>
  </Actions>
</Task>
"""
    changed = not path.is_file() or read_unit(path) != text
    if changed:
        write_text_file(path, text, encoding="utf-16")
    task = ["/TN", SERVICE_LABEL]
    if changed or not _run_service_command(["schtasks", "/Query", *task], check=False):
        # /Create /F leaves a running proxy on its old command line, so end it first.
        _run_service_command(["schtasks", "/End", *task], check=False)
        _run_service_command(["schtasks", "/Create", *task, "/XML", str(path), "/F"])
    # IgnoreNew keeps this from starting a second proxy next to a running one.
    # shortcut: RestartOnFailure ignores a nonzero exit, so a crashed proxy waits for the next
    # login or launch; add a watchdog if that proves too slow.
    _run_service_command(["schtasks", "/Run", *task])


def _systemd_quote(value: str, *, exec_start: bool = False) -> str:
    if any(char in value for char in "\r\n"):
        raise ValueError("systemd unit values cannot contain newlines")
    escaped = value.replace("%", "%%")
    if exec_start:
        escaped = escaped.replace("$", "$$")
    escaped = escaped.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def allocate_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((LOOPBACK_HOST, 0))
        return int(sock.getsockname()[1])


def read_unit(path: Path) -> str:
    # UTF-16 is the encoding Task Scheduler exports and the only one schtasks documents reading.
    return path.read_text(encoding="utf-16" if current_os() == OS.WINDOWS else "utf-8")


def _owned(path: Path) -> bool:
    return path.is_file() and UNIT_MARKER in read_unit(path)


def install(
    workspace: str, port: int, relay_port: int | None = None, *, copilot_app: bool = False
) -> None:
    path = service_path()
    if path.is_symlink():
        raise RuntimeError(f"Refusing to replace a symlinked service file: {path}")
    if path.exists() and not _owned(path):
        raise RuntimeError(f"Refusing to replace an unmanaged service file: {path}")
    marker = f"{UNIT_MARKER} {COPILOT_APP_NOTE}" if copilot_app else UNIT_MARKER
    # Also serve the ChatGPT relay for the Codex app; before --port so its anchor still matches.
    relay = ["--relay-port", str(relay_port)] if relay_port else []
    if current_os() == OS.WINDOWS:
        # pythonw runs the proxy without a console window that closing would kill.
        # shortcut: the task gets the logon environment, not this shell's DATABRICKS_* overrides;
        # pass them through if a Windows user needs them.
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        python = str(pythonw if pythonw.is_file() else sys.executable)
        argv = [
            python,
            "-m",
            "ucode.cli",
            "otel-proxy",
            "--host",
            workspace,
            *relay,
            "--port",
            str(port),
            *workspace_id_args(workspace),
        ]
        _ensure_scheduled_task(path, argv, marker)
        return
    ug_path = ug_binary()
    if not os.path.isabs(ug_path):
        raise RuntimeError(
            "Could not resolve an absolute ug executable for the Codex OTEL service."
        )
    if current_os() == OS.MACOS:
        _ensure_launch_agent(
            path,
            [
                ug_path,
                "otel-proxy",
                "--host",
                workspace,
                *relay,
                "--port",
                str(port),
                *workspace_id_args(workspace),
            ],
            marker,
        )
        return
    lines = [
        marker,
        "[Service]",
        # systemd expands `$` in ExecStart arguments but never in the executable path.
        f"ExecStart={_systemd_quote(ug_path)} otel-proxy --host "
        f"{_systemd_quote(workspace, exec_start=True)} {' '.join([*relay, '--port', str(port)])}"
        + "".join(f" {_systemd_quote(arg)}" for arg in workspace_id_args(workspace)),
        "Restart=on-failure",
        "RestartSec=1s",
    ]
    for name, value in _service_env().items():
        lines.append(f"Environment={_systemd_quote(f'{name}={value}')}")
    lines.extend(["", "[Install]", "WantedBy=default.target", ""])
    text = "\n".join(lines)
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        # Every launch: start a stopped proxy without restarting a running one mid-export.
        # shortcut: a profile switch on one workspace waits for a service restart; restart if hit.
        _run_systemctl(["enable", "--now", SERVICE_NAME])
        return
    write_text_file(path, text)
    _run_systemctl(["daemon-reload"])
    _run_systemctl(["enable", SERVICE_NAME])
    _run_systemctl(["restart", SERVICE_NAME])


def remove() -> None:
    path = service_path()
    if path.is_symlink():
        raise RuntimeError(f"Refusing to remove a symlinked service file: {path}")
    if not _owned(path):
        return
    if current_os() == OS.MACOS:
        # An already-unloaded agent is fine to remove.
        _run_service_command(
            ["launchctl", "bootout", f"gui/{os.getuid()}/{SERVICE_LABEL}"], check=False
        )
        path.unlink(missing_ok=True)
        return
    if current_os() == OS.WINDOWS:
        # An already-stopped or deleted task is fine to remove.
        task = ["/TN", SERVICE_LABEL]
        _run_service_command(["schtasks", "/End", *task], check=False)
        _run_service_command(["schtasks", "/Delete", *task, "/F"], check=False)
        path.unlink(missing_ok=True)
        return
    _run_systemctl(["disable", "--now", SERVICE_NAME])
    path.unlink(missing_ok=True)
    _run_systemctl(["daemon-reload"])


def copilot_app_recorded() -> bool:
    """Whether ug's own login service unit records that the GitHub Copilot app needs it."""
    unit = service_path()
    return _owned(unit) and COPILOT_APP_NOTE in read_unit(unit)


def recorded_port(flag: str) -> int | None:
    """The ``--{flag}`` port recorded in ug's own login service unit, if there is one."""
    unit = service_path()
    text = read_unit(unit) if _owned(unit) else ""
    match = re.search(rf" --{flag} (\d+)(?= --|$)", text, re.MULTILINE)
    # A plist puts the port in its own <string>; task XML ends it with </Arguments>.
    match = match or re.search(rf"--{flag}\D+(\d+)", text)
    return int(match[1]) if match else None


def serve(host: str, workspace_id: str | None, port: int, relay_port: int | None) -> int:
    """Run the service's listeners until it is stopped; returns an exit code on failure."""
    from ucode import gateway_proxy

    try:
        state = load_full_state().get("workspaces", {}).get(workspace_key(host, workspace_id))
        if workspace_id:
            pin(host, workspace_id)
        if not state:
            raise RuntimeError(f"No saved configuration for {host}. Run `ug configure` first.")
        apply_pat_environment(state)

        def token_provider(force: bool) -> str:
            return launch_token(state, host, force_refresh=force)

        from ucode.agents import copilot

        # One port for every desktop agent: Codex's trace exporter keeps its /v1/ URL, and the
        # GitHub Copilot app's routes are always served.
        server, _cache, _client = gateway_proxy.start_service_listener(
            host,
            token_provider,
            port=port,
            exact_routes=copilot.app_service_routes(state),
            prefix_routes={"/v1/": gateway_proxy.ServiceRoute("/ai-gateway/otel/v1/")},
            sign_in_hint=f"Run `databricks auth login --host {host}`, then retry.",
        )
        if relay_port:
            # The Codex app is pinned to this port, so fail (and restart) rather than move.
            relay, _cache, _client = gateway_proxy.start_relay_proxy(
                host, token_provider, relay_port, agent=AGENT_CODEX, fallback_on_bind_error=False
            )
            threading.Thread(target=relay.serve_forever, daemon=True).start()
        # systemd stops this with SIGTERM, which exits without cleanup, and nothing needs it.
        server.serve_forever()
    except (OSError, RuntimeError) as exc:
        print_err(str(exc))
        return 1
    return 0
