"""Shared MCP backend. Each local socket owns an independent upstream session."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
import sys
import threading
import traceback
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import cast

import anyio
from anyio import to_thread

from ucode.databricks import get_databricks_token, resolve_pat_token
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

    async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal active, last_disconnect, selection
        active += 1
        io = _SessionIO(reader, writer)
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
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = _unwrap_proxy_error(exc)
            detail = str(error or exc)
            if error is None and not isinstance(exc, (RuntimeError, ValueError, ConnectionError)):
                traceback.print_exception(exc, file=sys.stderr)
                detail += f". See {directory / 'backend.log'}"
            with suppress(ConnectionError, OSError):
                await io.send({"error": detail})
        finally:
            writer.close()
            with suppress(ConnectionError, OSError):
                await writer.wait_closed()
            active -= 1
            last_disconnect = asyncio.get_running_loop().time()

    server = await asyncio.start_server(accept, "127.0.0.1", 0, limit=MAX_FRAME_BYTES)
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
        path.unlink(missing_ok=True)


def main() -> None:
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
