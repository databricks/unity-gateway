"""Small stdio relays discover and start a shared, authenticated local MCP backend.

Keep this module free of MCP, HTTP and CLI imports: each agent still needs a child
process for its private stdio pipes, but only the backend loads those libraries.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import BinaryIO

from ucode.os_compatibility import subprocess_cross_os
from ucode.os_compatibility.file_lock_cross_os import acquire_exclusive_file_lock, release_file_lock
from ucode.state_file import current_workspace_state, read_state_file

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 64 * 1024 * 1024
STARTUP_TIMEOUT_SECONDS = 20.0
IDLE_TIMEOUT_SECONDS = 60.0

# A backend may inherit credentials and CLI/network configuration, never another
# client's request headers. External bearer commands can depend on arbitrary env.
_RUNTIME_ENV_KEYS = {
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "BROWSER",
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "DBUS_SESSION_BUS_ADDRESS",
    "PYTHONPATH",
    "PYTHONHOME",
}


def runtime_directory(workspace: str | None = None, workspace_id: str | None = None) -> Path:
    from ucode.entrypoint import _read_installed_version

    external_command = bool(os.environ.get("DATABRICKS_BEARER_COMMAND", "").strip())
    environment = {
        key: value
        for key, value in os.environ.items()
        if (key != "UG_HEADERS" or external_command)
        and (
            external_command or key in _RUNTIME_ENV_KEYS or key.startswith(("DATABRICKS_", "XDG_"))
        )
    }
    package = Path(__file__).parent
    relative_configuration = any(
        environment.get(name) and not os.path.isabs(environment[name])
        for name in (
            "DATABRICKS_CONFIG_FILE",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "REQUESTS_CA_BUNDLE",
            "CURL_CA_BUNDLE",
        )
    ) or any(not os.path.isabs(part) for part in environment.get("PATH", "").split(os.pathsep))
    sources = [
        "mcp_proxy_runtime.py",
        "mcp_proxy_daemon.py",
        "mcp_proxy.py",
        "databricks.py",
        "mcp_connection_login.py",
    ]
    identity = {
        "protocol": PROTOCOL_VERSION,
        "python": sys.executable,
        "package": str(package),
        "version": _read_installed_version(),
        "sources": [package.joinpath(name).stat().st_mtime_ns for name in sources],
        "environment": environment,
        # Workspace selection is process-wide in the existing routing helpers.
        "workspace_selection": [workspace, workspace_id] if workspace_id else None,
        # Relative external bearer commands must run in the caller's directory.
        "cwd": os.getcwd()
        if environment.get("DATABRICKS_BEARER_COMMAND") or relative_configuration
        else None,
    }
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:32]
    return Path.home() / ".ucode" / "mcp-proxy" / key


def private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or (
        os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077)
    ):
        raise RuntimeError(f"MCP runtime directory must be private to your user: {path}")


def _connect(directory: Path) -> socket.socket | None:
    try:
        metadata = json.loads((directory / "endpoint.json").read_text(encoding="utf-8"))
        if not isinstance(metadata, dict) or metadata.get("protocol") != PROTOCOL_VERSION:
            return None
        if not isinstance(metadata.get("port"), int) or not 0 < metadata["port"] < 65536:
            return None
        if not isinstance(metadata.get("secret"), str) or len(metadata["secret"]) != 64:
            return None
        sock = socket.create_connection(("127.0.0.1", metadata["port"]), timeout=1.0)
        try:
            sock.sendall(json.dumps({"secret": metadata["secret"]}).encode() + b"\n")
            # The handshake has one short response; never consume MCP bytes here.
            with sock.makefile("rb") as reader:
                response = reader.readline(1024)
            if json.loads(response) != {"ready": True}:
                sock.close()
                return None
            sock.settimeout(None)
            return sock
        except BaseException:
            sock.close()
            raise
    except (OSError, ValueError, KeyError, TypeError):
        return None


def connect_backend(directory: Path) -> socket.socket:
    private_directory(directory.parent)
    private_directory(directory)
    if connection := _connect(directory):
        return connection
    # Keep the lock inode: unlinking it lets concurrent launchers lock different files.
    with (directory / "start.lock").open("a+b") as lock:
        acquire_exclusive_file_lock(lock, timeout=STARTUP_TIMEOUT_SECONDS)
        try:
            if connection := _connect(directory):
                return connection
            log_fd = os.open(
                directory / "backend.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
            )
            with os.fdopen(log_fd, "ab") as log:
                options = (
                    {
                        "creationflags": subprocess.DETACHED_PROCESS
                        | subprocess.CREATE_NEW_PROCESS_GROUP
                    }
                    if os.name == "nt"
                    else {"start_new_session": True}
                )
                child = subprocess_cross_os.popen(
                    [sys.executable, "-m", "ucode.mcp_proxy_daemon", str(directory)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=log,
                    env={
                        key: value
                        for key, value in os.environ.items()
                        if key != "UG_HEADERS"
                        or os.environ.get("DATABRICKS_BEARER_COMMAND", "").strip()
                    },
                    **options,
                )
            deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                if connection := _connect(directory):
                    # Reap on idle exit without keeping the agent's relay alive.
                    threading.Thread(target=child.wait, daemon=True).start()
                    return connection
                if child.poll() is not None:
                    # Another daemon may have won the lifetime lock after a launcher died.
                    if connection := _connect(directory):
                        return connection
                    break
                time.sleep(0.05)
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
            raise RuntimeError(f"Shared MCP proxy did not start. See {directory / 'backend.log'}")
        finally:
            release_file_lock(lock)


def relay(sock: socket.socket, stdin: BinaryIO, stdout: BinaryIO, stderr: BinaryIO) -> None:
    input_closed = threading.Event()

    def send_input() -> None:
        try:
            # A daemon thread blocked in BufferedReader.read prevents CPython's
            # shutdown from acquiring the stdin lock after a backend crash.
            while chunk := os.read(stdin.fileno(), 65536):
                sock.sendall(chunk)
            input_closed.set()
        except OSError:
            pass
        finally:
            # EOF means the agent ended this session; don't leave an orphan upstream.
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    threading.Thread(target=send_input, daemon=True).start()
    with sock.makefile("rb") as reader:
        while True:
            try:
                frame = reader.readline(MAX_FRAME_BYTES + 1)
            except ConnectionError:
                # A terminated backend produces a reset on Windows and EOF on Unix.
                break
            if not frame:
                break
            if len(frame) > MAX_FRAME_BYTES:
                raise RuntimeError("Shared MCP proxy response exceeded the message limit")
            envelope = json.loads(frame)
            if "message" in envelope:
                stdout.write(envelope["message"].encode("utf-8") + b"\n")
                stdout.flush()
            elif "stderr" in envelope:
                stderr.write(envelope["stderr"].encode("utf-8"))
                stderr.flush()
            elif "error" in envelope:
                raise RuntimeError(envelope["error"])
        if not input_closed.is_set():
            raise RuntimeError(
                "Shared MCP proxy disconnected. Reconnect the MCP server to start a new session."
            )


def serve(
    url: str,
    workspace: str,
    profile: str | None = None,
    *,
    use_pat: bool = False,
    workspace_id: str | None = None,
) -> None:
    try:
        with connect_backend(runtime_directory(workspace, workspace_id)) as sock:
            sock.sendall(
                json.dumps(
                    {
                        "url": url,
                        "workspace": workspace,
                        "profile": profile,
                        "use_pat": use_pat,
                        "workspace_id": workspace_id,
                        "headers": os.environ.get("UG_HEADERS", ""),
                    }
                ).encode()
                + b"\n"
            )
            relay(sock, sys.stdin.buffer, sys.stdout.buffer, sys.stderr.buffer)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ug mcp-proxy: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(2) from None


def load_proxy_state() -> dict:
    return current_workspace_state(read_state_file(Path.home() / ".ucode" / "state.json"))


def run_proxy(
    url: str,
    host: str | None = None,
    profile: str | None = None,
    use_pat: bool = False,
    workspace_id: str | None = None,
) -> None:
    state = load_proxy_state()
    workspace = host or state.get("workspace")
    if not workspace:
        print("No workspace configured. Run `ug configure` first.", file=sys.stderr)
        raise SystemExit(1)
    if not workspace_id and workspace == state.get("workspace"):
        workspace_id = state.get("workspace_id")
    options = {"workspace_id": workspace_id} if workspace_id else {}
    serve(
        url,
        workspace,
        profile or state.get("profile"),
        use_pat=use_pat or bool(state.get("use_pat")),
        **options,
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog=f"{sys.argv[0]} mcp-proxy", allow_abbrev=False)
    parser.add_argument("--url", required=True, help="Databricks streamable-HTTP MCP endpoint.")
    parser.add_argument("--host", help="Workspace URL; defaults to the saved workspace.")
    parser.add_argument("--profile", help="Databricks CLI profile.")
    parser.add_argument("--workspace-id", help=argparse.SUPPRESS)
    parser.add_argument(
        "--use-pat", action="store_true", help="Use the profile's personal access token."
    )
    run_proxy(**vars(parser.parse_args(sys.argv[2:])))
