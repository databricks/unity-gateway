"""Linux user-systemd service for the Codex OTLP loopback proxy."""

from __future__ import annotations

import os
import platform
import shutil
import signal
import socket
import tempfile
import threading
from pathlib import Path
from typing import Any

from ucode import config_io
from ucode.databricks import apply_pat_environment, ug_binary
from ucode.os_compatibility import subprocess_cross_os

SERVICE_NAME = "ug-codex-otel.service"
RUNTIME_FILENAME = "codex-otel-service.json"
SERVICE_DESCRIPTION = "Unity Gateway Codex OTEL proxy"
SERVICE_START_TIMEOUT_SECONDS = 180
_UNIT_MARKER = "# Managed by unity-gateway; do not edit."
_RUNTIME_KEYS = frozenset({"workspace", "profile", "use_pat", "custom_oauth", "port"})
_CUSTOM_OAUTH_KEYS = frozenset({"client_id", "redirect_url", "scopes", "profile"})


def runtime_path() -> Path:
    """Use the current APP_DIR so isolated callers can redirect it."""
    return config_io.APP_DIR / RUNTIME_FILENAME


def unit_path() -> Path:
    """Return the per-user unit path."""
    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        return Path(config_home).expanduser() / "systemd" / "user" / SERVICE_NAME
    return Path.home() / ".config" / "systemd" / "user" / SERVICE_NAME


def _require_linux() -> None:
    if platform.system() != "Linux":
        raise RuntimeError("The Codex OTEL systemd service is supported only on Linux.")


def _normalise_custom_oauth(value: object, *, strict: bool) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("custom_oauth must be an object")
    if strict and not set(value).issubset(_CUSTOM_OAUTH_KEYS):
        raise ValueError("custom_oauth contains unsupported fields")

    client_id, redirect_url, scopes = (
        value.get("client_id"),
        value.get("redirect_url"),
        value.get("scopes"),
    )
    if not isinstance(client_id, str) or not client_id.strip():
        raise ValueError("custom_oauth.client_id must be a non-empty string")
    if not isinstance(redirect_url, str) or not redirect_url.strip():
        raise ValueError("custom_oauth.redirect_url must be a non-empty string")
    if not isinstance(scopes, (list, tuple)) or not all(
        isinstance(s, str) and s.strip() for s in scopes
    ):
        raise ValueError("custom_oauth.scopes must be a list of non-empty strings")

    result: dict[str, Any] = {
        "client_id": client_id,
        "redirect_url": redirect_url,
        "scopes": list(scopes),
    }
    profile = value.get("profile")
    if profile is not None:
        if not isinstance(profile, str) or not profile.strip():
            raise ValueError("custom_oauth.profile must be a string when provided")
        result["profile"] = profile
    return result


def _normalise_state(state: object, *, strict_custom_oauth: bool = False) -> dict[str, Any]:
    if not isinstance(state, dict):
        raise ValueError("Codex OTEL service state must be an object")

    workspace = state.get("workspace")
    if not isinstance(workspace, str) or not workspace.strip():
        raise ValueError("A workspace is required for the Codex OTEL service")
    profile = state.get("profile")
    if profile is not None and (not isinstance(profile, str) or not profile.strip()):
        raise ValueError("profile must be a string when provided")
    use_pat = state.get("use_pat", False)
    if not isinstance(use_pat, bool):
        raise ValueError("use_pat must be a boolean")

    custom_oauth = _normalise_custom_oauth(state.get("custom_oauth"), strict=strict_custom_oauth)
    return {
        "workspace": workspace,
        "profile": profile,
        "use_pat": use_pat,
        "custom_oauth": custom_oauth,
    }


def _validate_record(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) != _RUNTIME_KEYS:
        return None
    try:
        state = _normalise_state(value, strict_custom_oauth=True)
    except ValueError:
        return None
    port = value.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        return None
    return {**state, "port": port}


def _load_record() -> dict[str, Any] | None:
    return _validate_record(config_io.read_json_safe(runtime_path()))


def configured_endpoint() -> str | None:
    if platform.system() != "Linux":
        return None
    record = _load_record()
    if record is None:
        return None
    return _endpoint(record["port"])


def _endpoint(port: int) -> str:
    return f"http://127.0.0.1:{port}/v1/traces"


def _allocate_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _systemctl_path() -> str:
    path = shutil.which("systemctl")
    if not path:
        raise RuntimeError(
            "Codex OTEL tracing requires the per-user systemd `systemctl` executable."
        )
    return path


def _systemctl_error_is_absent(detail: str) -> bool:
    text = detail.lower()
    return any(
        marker in text
        for marker in (
            "not loaded",
            "not found",
            "no such file",
            "doesn't exist",
            "does not exist",
            "could not be found",
        )
    )


def _run_systemctl(args: list[str], *, allow_absent: bool = False) -> None:
    command = [_systemctl_path(), "--user", *args]
    try:
        result = subprocess_cross_os.run(command, check=False, capture_output=True, text=True)
    except OSError as exc:
        raise RuntimeError(f"Could not run systemctl --user: {exc}") from exc
    if result.returncode == 0:
        return
    detail = (result.stderr or result.stdout or "").strip()
    if allow_absent and _systemctl_error_is_absent(detail):
        return
    operation = " ".join(args)
    suffix = f": {detail}" if detail else ""
    raise RuntimeError(f"systemctl --user {operation} failed (exit {result.returncode}){suffix}")


def _systemd_quote(value: str, *, exec_start: bool = False) -> str:
    if any(char in value for char in "\r\n"):
        raise ValueError("systemd unit values cannot contain newlines")
    escaped = value.replace("%", "%%")
    if exec_start:
        escaped = escaped.replace("$", "$$")
    escaped = escaped.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _unit_text() -> str:
    ug_path = ug_binary()
    if not os.path.isabs(ug_path):
        raise RuntimeError(
            "Could not resolve an absolute ug executable for the Codex OTEL service."
        )
    lines = [
        _UNIT_MARKER,
        "[Unit]",
        f"Description={_systemd_quote(SERVICE_DESCRIPTION)}",
        "",
        "[Service]",
        "Type=notify",
        f"ExecStart={_systemd_quote(ug_path, exec_start=True)} otel-proxy",
        "Restart=on-failure",
        "RestartSec=1s",
        f"TimeoutStartSec={SERVICE_START_TIMEOUT_SECONDS}s",
        "NotifyAccess=main",
    ]
    # User services do not inherit the configuring shell's CLI/auth environment.
    environment = {
        "PATH": os.environ.get("PATH"),
        "DATABRICKS_CONFIG_FILE": os.environ.get("DATABRICKS_CONFIG_FILE"),
        "DATABRICKS_AUTH_STORAGE": os.environ.get("DATABRICKS_AUTH_STORAGE"),
    }
    if environment["DATABRICKS_CONFIG_FILE"]:
        environment["DATABRICKS_CONFIG_FILE"] = str(
            Path(environment["DATABRICKS_CONFIG_FILE"]).expanduser().absolute()
        )
    for name, value in environment.items():
        if value:
            lines.append(f"Environment={_systemd_quote(f'{name}={value}')}")
    lines += ["", "[Install]", "WantedBy=default.target", ""]
    return "\n".join(lines)


def _atomic_write_text(path: Path, content: str) -> None:
    config_io.ensure_parent_dir(path)
    fd, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary_name, path)
    except OSError as exc:
        Path(temporary_name).unlink(missing_ok=True)
        raise RuntimeError(f"Failed to write systemd unit: {path}") from exc


def _owned_unit(path: Path) -> bool:
    try:
        return path.is_file() and _UNIT_MARKER in path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False


def _rollback_new_install(unit: Path, runtime: Path) -> list[str]:
    errors: list[str] = []
    try:
        _run_systemctl(["disable", "--now", SERVICE_NAME], allow_absent=True)
    except Exception as exc:
        errors.append(str(exc))
    for path, label in ((unit, "unit"), (runtime, "runtime record")):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            errors.append(f"failed to remove {label} {path}: {exc}")
    try:
        _run_systemctl(["daemon-reload"])
    except Exception as exc:
        errors.append(str(exc))
    return errors


def ensure(state: dict) -> str:
    _require_linux()
    try:
        normalized = _normalise_state(state)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc

    existing = _load_record()
    port = existing["port"] if existing is not None else _allocate_port()
    record = {**normalized, "port": port}
    if config_io.is_dry_run():
        return _endpoint(port)

    path = unit_path()
    existing_unit = path.exists()
    if existing_unit and not _owned_unit(path):
        raise RuntimeError(f"Refusing to replace an unmanaged systemd unit: {path}")
    unit = _unit_text()
    unit_changed = not existing_unit or path.read_text(encoding="utf-8") != unit

    _systemctl_path()
    new_install = existing is None and not existing_unit
    try:
        if unit_changed:
            _atomic_write_text(path, unit)
        if existing != record:
            config_io.atomic_write_json(runtime_path(), record)
        _run_systemctl(["daemon-reload"])
        _run_systemctl(["enable", SERVICE_NAME])

        _run_systemctl(["restart", SERVICE_NAME])
    except Exception as exc:
        if not new_install:
            raise
        rollback_errors = _rollback_new_install(path, runtime_path())
        if rollback_errors:
            detail = "; ".join(rollback_errors)
            raise RuntimeError(f"{exc} (initial service rollback also failed: {detail})") from exc
        raise
    return _endpoint(port)


def remove() -> None:
    if platform.system() != "Linux":
        return
    if config_io.is_dry_run():
        return

    unit = unit_path()
    runtime = runtime_path()
    owned = _owned_unit(unit)
    runtime_exists = runtime.exists()
    if not owned and not runtime_exists:
        return
    if owned:
        _run_systemctl(["disable", "--now", SERVICE_NAME], allow_absent=True)
        try:
            unit.unlink()
        except OSError as exc:
            raise RuntimeError(f"Failed to remove systemd unit: {unit}") from exc
    if runtime_exists:
        try:
            runtime.unlink()
        except OSError as exc:
            raise RuntimeError(f"Failed to remove OTEL runtime record: {runtime}") from exc
    if owned:
        _run_systemctl(["daemon-reload"])


def _sd_notify(message: str) -> None:
    notify_socket = os.environ.get("NOTIFY_SOCKET")
    if not notify_socket:
        return
    address: str = "\0" + notify_socket[1:] if notify_socket.startswith("@") else notify_socket
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.sendto(message.encode("utf-8"), address)
    except OSError as exc:
        raise RuntimeError(f"Could not notify systemd that the OTEL proxy is ready: {exc}") from exc
    finally:
        sock.close()


def _cleanup_proxy(
    server: Any,
    cache: Any,
    client: Any,
    server_thread: threading.Thread | None,
) -> None:
    actions = [cache.stop]
    if server_thread is not None:
        actions.append(server.shutdown)
    actions.append(server.server_close)
    if server_thread is not None:
        actions.append(lambda: server_thread.join(timeout=5))
    actions.append(client.close)
    errors: list[BaseException] = []
    for action in actions:
        try:
            action()
        except BaseException as exc:
            errors.append(exc)
    if server_thread is not None and server_thread.is_alive():
        errors.append(RuntimeError("The Codex OTEL proxy did not stop cleanly."))
    if errors:
        raise errors[0]


def run() -> None:
    _require_linux()
    record = _load_record()
    if record is None:
        raise RuntimeError(f"Missing or invalid Codex OTEL runtime record: {runtime_path()}")

    from ucode import gateway_proxy
    from ucode.agents import codex

    apply_pat_environment(record)
    workspace = record["workspace"]
    server, cache, client = gateway_proxy.start_otel_proxy(
        workspace, codex._otel_token_provider(record, workspace), port=record["port"]
    )
    server_thread: threading.Thread | None = None
    old_handlers: dict[int, Any] = {}
    stop_event = threading.Event()
    serve_errors: list[BaseException] = []

    def _handle_signal(_signum: int, _frame: Any) -> None:
        stop_event.set()

    def _serve() -> None:
        try:
            server.serve_forever()
        except BaseException as exc:
            serve_errors.append(exc)

    can_install_handlers = threading.current_thread() is threading.main_thread()
    if can_install_handlers:
        for signum in (signal.SIGTERM, signal.SIGINT):
            old_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, _handle_signal)
    try:
        thread = threading.Thread(target=_serve, name="codex-otel", daemon=True)
        thread.start()
        server_thread = thread
        _sd_notify("READY=1")
        while server_thread.is_alive():
            server_thread.join(timeout=0.25)
            if stop_event.is_set():
                break
        if serve_errors:
            raise serve_errors[0]
    finally:
        try:
            _cleanup_proxy(server, cache, client, server_thread)
        finally:
            for signum, handler in old_handlers.items():
                signal.signal(signum, handler)
