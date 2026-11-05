"""Component journeys through real relay/backend processes and a local MCP HTTP peer.

The HTTP peer supplies deterministic protocol responses instead of requiring a
workspace. The relay, shared backend, MCP SDK and socket lifecycle stay real.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import queue
import signal
import socket
import struct
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import anyio
import pytest

from ucode import mcp_proxy, mcp_proxy_daemon, mcp_proxy_runtime


class _Client:
    def __init__(
        self,
        env,
        url,
        profile="component",
        *,
        workspace="https://component.example",
        workspace_id=None,
    ):
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from ucode.entrypoint import main; main()",
                "mcp-proxy",
                "--url",
                url,
                "--host",
                workspace,
                "--profile",
                profile,
                *(["--workspace-id", workspace_id] if workspace_id else []),
            ],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self.messages = queue.Queue()

        def receive():
            for line in self.process.stdout:
                self.messages.put(json.loads(line))
            self.messages.put(None)

        threading.Thread(target=receive, daemon=True).start()

    def send(self, value):
        self.process.stdin.write(json.dumps(value) + "\n")
        self.process.stdin.flush()

    def receive(self):
        value = self.messages.get(timeout=15)
        assert value is not None, self.process.stderr.read()
        return value

    def initialize(self, client_name="component"):
        self.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": client_name, "version": "1"},
                },
            }
        )
        reply = self.receive()
        assert reply["id"] == 1
        assert reply["result"]["serverInfo"]["name"] == "component-peer"
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, value, request_id=2):
        self.send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {
                    "name": "echo",
                    "arguments": {"value": value},
                },
            }
        )
        reply = self.receive()
        assert reply["id"] == request_id
        return json.loads(reply["result"]["content"][0]["text"])

    def close(self):
        if not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
            pytest.fail("stdio relay did not exit after its agent closed stdin")


@pytest.fixture
def peer():
    class Sessions(set):
        def __init__(self):
            super().__init__()
            self.user_agents = {}

    sessions = Sessions()
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def reply(self, status, body=None, session=None):
            encoded = json.dumps(body).encode() if body is not None else b""
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            if session:
                self.send_header("Mcp-Session-Id", session)
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            self.reply(405)

        def do_DELETE(self):
            with lock:
                sessions.discard(self.headers.get("Mcp-Session-Id"))
            self.reply(204)

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            method = request.get("method", "")
            if "id" not in request:
                self.reply(202)
                return
            session = self.headers.get("Mcp-Session-Id")
            if method == "initialize":
                with lock:
                    session = f"session-{len(sessions) + 1}-{self.client_address[1]}"
                    sessions.add(session)
                result = {
                    "protocolVersion": request["params"]["protocolVersion"],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "component-peer", "version": "1"},
                }
            elif method == "tools/call":
                with lock:
                    assert session in sessions
                result = {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "session": session,
                                    "path": self.path,
                                    "value": request["params"]["arguments"]["value"],
                                    "header": self.headers.get("X-Agent"),
                                    "authorization": self.headers.get("Authorization"),
                                    "workspace_id": self.headers.get("X-Databricks-Workspace-Id"),
                                }
                            ),
                        }
                    ]
                }
            else:
                result = {"tools": []} if method == "tools/list" else {}
            with lock:
                sessions.user_agents.setdefault(session, []).append(self.headers.get("User-Agent"))
            self.reply(200, {"jsonrpc": "2.0", "id": request["id"], "result": result}, session)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", sessions
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def clients(tmp_path):
    # Explicitly replace inherited credentials; no developer auth is read.
    env = {key: value for key, value in os.environ.items() if not key.startswith("DATABRICKS_")}
    env.update(
        {
            "HOME": str(tmp_path),
            "USERPROFILE": str(tmp_path),
            "DATABRICKS_BEARER": "component-bearer",
            "NO_PROXY": "127.0.0.1,localhost",
            "PYTHONPATH": str(Path(mcp_proxy_runtime.__file__).parents[1]),
        }
    )
    started = []

    def start(
        url,
        *,
        header="",
        profile="component",
        bearer="component-bearer",
        workspace="https://component.example",
        workspace_id=None,
    ):
        client = _Client(
            {**env, "UG_HEADERS": header, "DATABRICKS_BEARER": bearer},
            url,
            profile,
            workspace=workspace,
            workspace_id=workspace_id,
        )
        started.append(client)
        return client

    yield start
    for client in started:
        client.close()
    for path in (tmp_path / ".ucode" / "mcp-proxy").glob("*/endpoint.json"):
        metadata = json.loads(path.read_text())
        try:
            os.kill(metadata["pid"], signal.SIGTERM)
        except OSError as exc:
            # Windows reports an already-exited PID as ERROR_INVALID_PARAMETER.
            if not isinstance(exc, ProcessLookupError) and getattr(exc, "winerror", None) != 87:
                raise


def _endpoints(tmp_path):
    return [
        json.loads(path.read_text())
        for path in (tmp_path / ".ucode" / "mcp-proxy").glob("*/endpoint.json")
    ]


def test_concurrent_agents_share_backend_with_distinct_sessions(clients, peer, tmp_path):
    url, sessions = peer
    first = clients(url + "/first", header="X-Agent: first", profile="one")
    second = clients(url + "/second", header="X-Agent: second", profile="two")
    # Both agents use request IDs 1 and 2; response routing must remain private.
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(
            pool.map(
                lambda item: item[0].initialize(item[1]),
                [(first, "first-agent"), (second, "second-agent")],
            )
        )
        responses = list(
            pool.map(lambda item: item[0].call(item[1]), [(first, "α"), (second, "β")])
        )
    assert len(_endpoints(tmp_path)) == 1
    assert responses[0]["session"] != responses[1]["session"]
    assert [(r["path"], r["value"], r["header"]) for r in responses] == [
        ("/first", "α", "first"),
        ("/second", "β", "second"),
    ]
    assert all(r["authorization"] == "Bearer component-bearer" for r in responses)
    for response, name in zip(responses, ["first-agent", "second-agent"], strict=True):
        assert len(sessions.user_agents[response["session"]]) == 2
        assert all(
            agent == mcp_proxy.ucode_user_agent(mcp_proxy.ug_version(), f"{name}/1")
            for agent in sessions.user_agents[response["session"]]
        )
    first.close()
    assert second.call("still alive", 3)["value"] == "still alive"


def test_explicit_user_agent_takes_precedence_over_client_info(clients, peer):
    url, sessions = peer
    client = clients(url + "/mcp", header="uSeR-aGeNt: custom/1")
    client.initialize("agent-from-client-info")
    session = client.call("hello")["session"]

    assert sessions.user_agents[session] == ["custom/1", "custom/1"]


def test_different_credential_environments_use_separate_backends(clients, peer, tmp_path):
    url, _ = peer
    first = clients(url + "/mcp", bearer="credential-one")
    second = clients(url + "/mcp", bearer="credential-two")
    first.initialize()
    second.initialize()
    assert first.call("one")["authorization"] == "Bearer credential-one"
    assert second.call("two")["authorization"] == "Bearer credential-two"
    assert len(_endpoints(tmp_path)) == 2


@pytest.mark.parametrize("reset", [False, True], ids=["eof", "reset"])
def test_backend_disconnect_reports_reconnect(reset):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        with socket.create_connection(listener.getsockname(), timeout=3) as relay_socket:
            backend_socket, _ = listener.accept()
            if reset:
                # SO_LINGER's fields are unsigned shorts on Windows and ints on Unix.
                backend_socket.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_LINGER,
                    struct.pack("HH" if os.name == "nt" else "ii", 1, 0),
                )
            backend_socket.close()
            read_fd, write_fd = os.pipe()
            with os.fdopen(read_fd, "rb") as stdin:
                try:
                    with pytest.raises(RuntimeError, match="disconnected.*Reconnect"):
                        mcp_proxy_runtime.relay(relay_socket, stdin, io.BytesIO(), io.BytesIO())
                finally:
                    # Unblock the relay's reader before closing its fd on Windows.
                    os.close(write_fd)


def test_backend_crash_fails_session_and_next_agent_restarts(clients, peer, tmp_path):
    url, _ = peer
    first = clients(url + "/mcp")
    first.initialize()
    (metadata,) = _endpoints(tmp_path)
    os.kill(metadata["pid"], signal.SIGTERM)
    assert first.process.wait(timeout=8) == 2
    assert "disconnected" in first.process.stderr.read()
    second = clients(url + "/mcp")
    second.initialize()
    assert second.call("restarted")["value"] == "restarted"
    (replacement,) = _endpoints(tmp_path)
    assert replacement["pid"] != metadata["pid"]


def test_workspace_selectors_are_isolated_on_the_same_host(clients, peer, tmp_path):
    host, _ = peer
    first = clients(host + "/mcp", workspace=host, workspace_id="101")
    second = clients(host + "/mcp", workspace=host, workspace_id="202")
    third = clients(host + "/mcp", workspace=host, workspace_id="101")
    for client in [first, second, third]:
        client.initialize()
    assert [client.call("routing")["workspace_id"] for client in [first, second, third]] == [
        "101",
        "202",
        "101",
    ]
    assert len(_endpoints(tmp_path)) == 2


def test_private_endpoint_rejects_unauthenticated_local_connections(clients, peer, tmp_path):
    first = clients(peer[0] + "/mcp")
    first.initialize()
    (metadata,) = _endpoints(tmp_path)
    with socket.create_connection(("127.0.0.1", metadata["port"]), timeout=2) as sock:
        sock.sendall(b'{"secret":"incorrect"}\n')
        assert sock.recv(1024) == b""
    assert first.call("authenticated")["value"] == "authenticated"
    if os.name != "nt":
        (path,) = (tmp_path / ".ucode" / "mcp-proxy").glob("*/endpoint.json")
        assert path.stat().st_mode & 0o077 == 0
        assert path.parent.stat().st_mode & 0o077 == 0


def test_backend_exits_after_last_client_disconnects(tmp_path):
    directory = tmp_path / "idle-backend"
    directory.mkdir()

    async def scenario():
        backend = asyncio.create_task(mcp_proxy_daemon.run_backend(directory, idle_timeout=0.15))
        async with asyncio.timeout(5):
            while not (directory / "endpoint.json").exists():
                await asyncio.sleep(0.01)
            metadata = json.loads((directory / "endpoint.json").read_text())
            reader, writer = await asyncio.open_connection("127.0.0.1", metadata["port"])
            writer.write(json.dumps({"secret": metadata["secret"]}).encode() + b"\n")
            await writer.drain()
            assert json.loads(await reader.readline()) == {"ready": True}
            # A connected agent keeps the backend alive, even before session setup.
            await asyncio.sleep(0.2)
            assert not backend.done()
            writer.close()
            await writer.wait_closed()
            await backend
        assert not (directory / "endpoint.json").exists()

    asyncio.run(scenario())


def test_pat_tokens_are_scoped_to_each_session(monkeypatch):
    # Replace only credential resolution: production providers and auth hooks run.
    monkeypatch.delenv("DATABRICKS_BEARER", raising=False)
    monkeypatch.setattr(mcp_proxy_daemon, "resolve_pat_token", lambda profile: f"pat-{profile}")
    first = mcp_proxy_daemon.session_token_provider("https://one", "one", True)
    second = mcp_proxy_daemon.session_token_provider("https://two", "two", True)

    async def scenario():
        async def authorize(provider):
            request = mcp_proxy._httpx().Request("POST", "https://component.example/mcp")
            auth = mcp_proxy._build_token_auth(
                str(request.url),
                "https://component.example",
                None,
                use_pat=True,
                token_provider=provider,
            )
            flow = auth.async_auth_flow(request)
            try:
                return (await anext(flow)).headers["Authorization"]
            finally:
                await flow.aclose()

        tokens = await asyncio.gather(authorize(first), authorize(second))
        assert tokens == ["Bearer pat-one", "Bearer pat-two"]

    asyncio.run(scenario())
    assert "DATABRICKS_BEARER" not in os.environ


def test_login_output_reaches_only_its_own_client():
    async def scenario():
        accepted = asyncio.Queue()

        async def accept(reader, writer):
            await accepted.put(mcp_proxy_daemon._SessionIO(reader, writer))

        server = await asyncio.start_server(accept, "127.0.0.1", 0)
        async with server:
            first_reader, first_writer = await asyncio.open_connection(
                "127.0.0.1", server.sockets[0].getsockname()[1]
            )
            first = await accepted.get()
            second_reader, second_writer = await asyncio.open_connection(
                "127.0.0.1", server.sockets[0].getsockname()[1]
            )
            second = await accepted.get()
            try:
                async with first.login_output() as output:
                    # A real subprocess writes to the fd the login CLI receives.
                    await anyio.to_thread.run_sync(
                        lambda: subprocess.run(
                            [sys.executable, "-c", "print('browser login URL')"],
                            stdin=subprocess.DEVNULL,
                            stdout=output,
                            stderr=output,
                            timeout=5,
                            check=True,
                        )
                    )
                    async with asyncio.timeout(3):
                        assert json.loads(await first_reader.readline()) == {
                            "stderr": "browser login URL" + os.linesep
                        }
                await second.send({"message": "second session"})
                assert json.loads(await second_reader.readline()) == {"message": "second session"}
            finally:
                for writer in [first_writer, second_writer, first.writer, second.writer]:
                    writer.close()
                    await writer.wait_closed()

    asyncio.run(scenario())


@pytest.mark.parametrize("contents", ["not JSON", "[]", '{"protocol":1,"port":-1}'])
def test_invalid_endpoint_metadata_is_recoverable(tmp_path, contents):
    (tmp_path / "endpoint.json").write_text(contents)
    assert mcp_proxy_runtime._connect(tmp_path) is None


def test_runtime_directory_cannot_be_a_symlink(tmp_path):
    destination = tmp_path / "private"
    destination.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(destination, target_is_directory=True)
    with pytest.raises(RuntimeError, match="private to your user"):
        mcp_proxy_runtime.private_directory(link)


def test_bad_session_does_not_stop_other_agents(clients, peer):
    good = clients(peer[0] + "/mcp")
    good.initialize()
    bad = clients("http://127.0.0.1:1/unreachable")
    bad.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert bad.process.wait(timeout=8) == 2
    assert good.call("survived")["value"] == "survived"
