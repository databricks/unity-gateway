"""Shared MCP backend. Each local socket owns an independent upstream session."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import logging.handlers
import os
import queue
import secrets
import sys
import threading
import time
import traceback
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import cast

import anyio
from anyio import to_thread

from ucode import tls
from ucode.databricks import get_databricks_token, resolve_pat_token
from ucode.env import env_bool
from ucode.mcp_proxy import ProxyAuthError, _run, _unwrap_proxy_error
from ucode.mcp_proxy_runtime import (
    IDLE_TIMEOUT_SECONDS,
    MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
    private_directory,
)
from ucode.os_compatibility.file_lock_cross_os import acquire_exclusive_file_lock, release_file_lock
from ucode.ug_headers import scoped_ug_headers
from ucode.workspace import pin

_DIAGNOSTIC_QUEUE_SIZE = 1024
_TRACE_LOG_MAX_BYTES = 1_000_000


class _PrivateRotatingFileHandler(logging.handlers.RotatingFileHandler):
    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        return os.fdopen(fd, "a", encoding="utf-8", newline="")


def _trace_logger(path: Path) -> logging.Logger:
    logger = logging.Logger("ucode.mcp_proxy.transport", level=logging.INFO)
    logger.addHandler(
        _PrivateRotatingFileHandler(
            path, maxBytes=_TRACE_LOG_MAX_BYTES, backupCount=1, encoding="utf-8", delay=True
        )
    )
    logger.propagate = False
    return logger


def _log_diagnostic(
    logger: logging.Logger,
    session_id: str,
    event: str,
    fields: dict,
    *,
    timestamp_ms: int | None = None,
) -> None:
    payload = {
        "timestamp_ms": timestamp_ms if timestamp_ms is not None else int(time.time() * 1000),
        "session_id": session_id,
        "event": event,
        **fields,
    }
    line = "mcp-diagnostic " + json.dumps(payload, separators=(",", ":"))
    if len(line) + 1 > _TRACE_LOG_MAX_BYTES:
        line = "mcp-diagnostic " + json.dumps(
            {
                "timestamp_ms": payload["timestamp_ms"],
                "session_id": "backend",
                "event": "diagnostic_dropped",
                "dropped_count": 1,
            },
            separators=(",", ":"),
        )
    with suppress(OSError, ValueError):
        logger.info("%s", line)


class _DiagnosticWriter:
    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger
        self._events: queue.Queue[tuple[str, str, dict, int] | None] = queue.Queue(
            maxsize=_DIAGNOSTIC_QUEUE_SIZE
        )
        self._drop_lock = threading.Lock()
        self._dropped = 0
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._write, daemon=True)
        self._thread.start()

    def record(self, session_id: str, event: str, fields: dict) -> None:
        with self._drop_lock:
            try:
                self._events.put_nowait((session_id, event, fields, int(time.time() * 1000)))
            except queue.Full:
                self._dropped += 1

    def _write(self) -> None:
        try:
            while (entry := self._events.get()) is not None:
                session_id, event, fields, timestamp_ms = entry
                _log_diagnostic(self._logger, session_id, event, fields, timestamp_ms=timestamp_ms)
                with self._drop_lock:
                    dropped, self._dropped = self._dropped, 0
                if dropped:
                    _log_diagnostic(
                        self._logger, "backend", "diagnostic_dropped", {"dropped_count": dropped}
                    )
                if self._closed.is_set() and self._events.empty():
                    return
        finally:
            with self._drop_lock:
                dropped, self._dropped = self._dropped, 0
            if dropped:
                _log_diagnostic(
                    self._logger, "backend", "diagnostic_dropped", {"dropped_count": dropped}
                )
            for handler in self._logger.handlers:
                handler.close()

    def close(self) -> None:
        self._closed.set()
        with suppress(queue.Full):
            self._events.put_nowait(None)
        self._thread.join()


def session_token_provider(workspace: str, profile: str | None, use_pat: bool) -> Callable[[], str]:
    pat: str | None = None

    def token() -> str:
        nonlocal pat
        # Never export a profile's PAT into the shared process environment.
        if use_pat and not os.environ.get("DATABRICKS_BEARER", "").strip():
            pat = pat or resolve_pat_token(profile)
            if not pat:
                raise ProxyAuthError(
                    f"--use-pat is set but no personal access token was found for profile '{profile or '<none>'}'. "
                    "Set DATABRICKS_BEARER, or reconfigure the profile."
                )
            return pat
        return get_databricks_token(workspace, profile)

    return token


class _SessionIO:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.lock = asyncio.Lock()

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        line = await self.reader.readline()
        if not line:
            raise StopAsyncIteration
        return line.decode("utf-8", errors="replace")

    async def send(self, envelope: dict) -> None:
        async with self.lock:
            self.writer.write(json.dumps(envelope, ensure_ascii=False).encode() + b"\n")
            await self.writer.drain()

    async def write(self, message: str) -> None:
        await self.send({"message": message.rstrip("\n")})

    async def flush(self) -> None:
        await self.writer.drain()

    @asynccontextmanager
    async def login_output(self):
        # The CLI needs a real fd for its live browser URL. Relay its output only
        # to this session, including while the blocking login runs in a worker.
        read_fd, write_fd = os.pipe()
        loop = asyncio.get_running_loop()

        def forward() -> None:
            with os.fdopen(read_fd, "rb") as source:
                while chunk := source.read1(8192):
                    future = asyncio.run_coroutine_threadsafe(
                        self.send({"stderr": chunk.decode("utf-8", errors="replace")}), loop
                    )
                    try:
                        future.result(timeout=5)
                    except Exception:
                        future.cancel()
                        break

        thread = threading.Thread(target=forward, daemon=True)
        thread.start()
        with os.fdopen(write_fd, "w", encoding="utf-8", buffering=1) as output:
            try:
                yield output
            finally:
                output.flush()


async def run_backend(directory: Path, *, idle_timeout: float = IDLE_TIMEOUT_SECONDS) -> None:
    secret = secrets.token_hex(32)
    active = 0
    last_disconnect = asyncio.get_running_loop().time()
    unset = object()
    selection: object = unset
    diagnostics: _DiagnosticWriter | None = None

    async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal active, last_disconnect, selection
        active += 1
        io = _SessionIO(reader, writer)
        session_id = secrets.token_hex(8)
        started = False

        def diagnostic(event: str, fields: dict) -> None:
            if diagnostics is not None:
                diagnostics.record(session_id, event, fields)

        try:
            async with asyncio.timeout(5):
                hello = json.loads(await reader.readline())
                supplied = hello.get("secret") if isinstance(hello, dict) else None
                if not isinstance(supplied, str) or not hmac.compare_digest(supplied, secret):
                    return
                await io.send({"ready": True})
                config = json.loads(await reader.readline())
                if not isinstance(config, dict):
                    raise ValueError("Invalid MCP session configuration")
                url, workspace = config.get("url"), config.get("workspace")
                profile = config.get("profile")
                headers = config.get("headers", "")
                use_pat = config.get("use_pat", False)
                workspace_id = config.get("workspace_id")
                if (
                    not isinstance(url, str)
                    or not isinstance(workspace, str)
                    or (profile is not None and not isinstance(profile, str))
                    or not isinstance(headers, str)
                    or not isinstance(use_pat, bool)
                    or (workspace_id is not None and not isinstance(workspace_id, str))
                ):
                    raise ValueError("Invalid MCP session configuration")
                requested_selection = (workspace, workspace_id) if workspace_id else None
                if selection is unset:
                    selection = requested_selection
                    if workspace_id:
                        pin(workspace, workspace_id)
                elif requested_selection != selection:
                    raise ValueError("MCP session uses a different workspace selection")
            diagnostic("session_start", {})
            started = True
            with scoped_ug_headers(headers):
                token_provider = session_token_provider(workspace, profile, use_pat)
                await to_thread.run_sync(token_provider)
                async with io.login_output() as login_output:
                    # Explicit streams bypass the SDK's process-wide stdio ownership.
                    await _run(
                        url,
                        workspace,
                        profile,
                        use_pat,
                        stdin=cast(anyio.AsyncFile[str], io),
                        stdout=cast(anyio.AsyncFile[str], io),
                        token_provider=token_provider,
                        login_output=login_output,
                        diagnostic=diagnostic if diagnostics is not None else None,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = _unwrap_proxy_error(exc)
            if started:
                diagnostic("session_error", {"error_type": type(error or exc).__name__})
            detail = str(error or exc)
            if error is None and not isinstance(exc, (RuntimeError, ValueError, ConnectionError)):
                traceback.print_exception(exc, file=sys.stderr)
                detail += f". See {directory / 'backend.log'}"
            with suppress(ConnectionError, OSError):
                await io.send({"error": detail})
        finally:
            if started:
                diagnostic("session_end", {})
            writer.close()
            with suppress(ConnectionError, OSError):
                await writer.wait_closed()
            active -= 1
            last_disconnect = asyncio.get_running_loop().time()

    server = await asyncio.start_server(accept, "127.0.0.1", 0, limit=MAX_FRAME_BYTES)
    if env_bool("UCODE_RELAYED_PROXY_DIAGNOSTICS"):
        diagnostics = _DiagnosticWriter(_trace_logger(directory / "transport.log"))
    metadata = {
        "protocol": PROTOCOL_VERSION,
        "port": server.sockets[0].getsockname()[1],
        "secret": secret,
        "pid": os.getpid(),
    }
    path = directory / "endpoint.json"
    temporary = directory / "endpoint.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(metadata, output)
    temporary.replace(path)
    try:
        async with server:
            while True:
                await asyncio.sleep(min(1.0, idle_timeout))
                if (
                    not active
                    and asyncio.get_running_loop().time() - last_disconnect >= idle_timeout
                ):
                    break
    finally:
        # The lifetime lock remains held through metadata removal.
        try:
            path.unlink(missing_ok=True)
        finally:
            if diagnostics is not None:
                await to_thread.run_sync(diagnostics.close)


def main() -> None:
    tls.apply()
    directory = Path(sys.argv[1])
    private_directory(directory)
    with (directory / "backend.lock").open("a+b") as lock:
        try:
            acquire_exclusive_file_lock(lock, timeout=0)
        except TimeoutError:
            return
        try:
            asyncio.run(run_backend(directory))
        finally:
            release_file_lock(lock)


if __name__ == "__main__":
    main()
