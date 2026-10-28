"""Desktop commands as child processes; HTTP and browser are controlled boundaries."""

import json
import os
import shlex
import subprocess
import sys
import textwrap
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from urllib.parse import parse_qs, urlsplit

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tests.integration.utils.harness import clean_environment

try:
    from mcp.shared.exceptions import MCPError
except ImportError:  # MCP 1.x uses the old class name.
    from mcp.shared.exceptions import McpError as MCPError

TOKEN = "desktop-process-test-bearer"
SERVICE = "system.ai.google_calendar"
TOOL = {"name": "read_calendar", "inputSchema": {"type": "object"}}


@pytest.fixture
def child_env(tmp_path, monkeypatch):
    home = tmp_path / "home with spaces"
    home.mkdir()
    env = clean_environment(home)
    env.update(
        DATABRICKS_BEARER=TOKEN,
        PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
        NO_PROXY="127.0.0.1,localhost",
    )
    # Generate the command from the same installation that runs these tests.
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
    monkeypatch.setenv("PATH", env["PATH"])
    return env


@pytest.fixture
def upstream():
    connected = Event()
    state = {
        "failure": "auth-code",
        "discovery_auth": False,
        "persistent": False,
        "calls": [],
        "browsers": [],
        "headers": [],
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, payload, status=200):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Databricks-Org-Id", "12345")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/mcp-service-login":
                state["browsers"].append(self.path)
                connected.set()
                self.respond({"signed_in": True})
                return
            state["headers"].append(self.headers.get("Authorization"))
            if path == "/ai-gateway/anthropic/v1/models":
                self.respond({"data": [{"id": "system.ai.claude-sonnet-5"}]})
            elif path.endswith("/user-credentials"):
                if not connected.is_set():
                    self.respond({"message": "not connected"}, 404)
                else:
                    self.respond({"provisioning_info": {"state": "ACTIVE"}})
            elif path.endswith("/" + SERVICE):
                self.respond(
                    {"config": {"source_connection": {"securable_kind": "CONNECTION_HTTP_DCR"}}}
                )
            else:
                self.respond({"message": "unexpected path"}, 404)

        def do_POST(self):
            state["headers"].append(self.headers.get("Authorization"))
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            request_id = payload.get("id")
            if request_id is None:
                self.respond({}, 202)
                return
            response = {"jsonrpc": "2.0", "id": request_id}
            method = payload["method"]
            if method == "initialize":
                response["result"] = {
                    "protocolVersion": payload["params"]["protocolVersion"],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "controlled-calendar", "version": "1"},
                }
            elif method == "tools/list":
                if state["discovery_auth"] and not connected.is_set():
                    response["error"] = {"code": -32006, "message": "Sign-in required"}
                else:
                    response["result"] = {"tools": [TOOL]}
            elif method == "tools/call":
                state["calls"].append(payload)
                if state["failure"] == "permission":
                    response["result"] = {
                        "isError": True,
                        "content": [{"type": "text", "text": "Permission denied from this IP"}],
                    }
                elif not connected.is_set() or state["persistent"]:
                    if state["failure"] == "http-401":
                        self.respond({"message": "Sign-in required"}, 401)
                        return
                    response["error"] = {"code": -32006, "message": "Sign-in required"}
                else:
                    response["result"] = {
                        "content": [{"type": "text", "text": json.dumps(payload["params"])}]
                    }
            else:
                response["error"] = {"code": -32601, "message": "Unexpected method"}
            self.respond(response)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["workspace"] = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert set(state["headers"]) == {f"Bearer {TOKEN}"}


@pytest.fixture
def generated_commands(upstream, child_env, tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""\
                import json
                import sys
                from pathlib import Path
                from unittest.mock import patch
                from ucode.agents.desktop import claude, cowork, setup

                root = claude.claude_desktop_root_directory()
                (root / "configLibrary").mkdir(parents=True)
                (root / "local-agent-mode-sessions" / "account" / "org").mkdir(parents=True)
                state = {"workspace": sys.argv[1], "profile": "selected-profile"}
                setup.configure_claude_desktop(state)
                # Native Claude plugin installation is outside this component test.
                with patch.object(cowork, "_install_plugin") as install:
                    setup.configure_claude_cowork(state, [{
                        "name": "calendar",
                        "url": sys.argv[1] + "/ai-gateway/mcp-services/system.ai.google_calendar",
                        "clients": ["claude"],
                    }])
                    install.assert_called_once()
                plugin = (Path.home() / ".ucode" / cowork.MARKETPLACE_DIRECTORY_NAME
                          / "plugins" / cowork.PLUGIN_NAME)
                profile = root / "configLibrary" / (claude.PROFILE_ID + ".json")
                print(json.dumps({
                    "profile": json.loads(profile.read_text()),
                    "mcp": json.loads((plugin / ".mcp.json").read_text()),
                }))
                """),
            upstream["workspace"],
        ],
        env=child_env,
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert TOKEN not in result.stdout + result.stderr
    return json.loads(result.stdout.splitlines()[-1])


@asynccontextmanager
async def proxy_client(generated_commands, child_env, tmp_path):
    # This replaces only the external browser; the proxy, HTTP auth and credential polling run.
    browser = tmp_path / "browser with spaces.py"
    browser.write_text(
        "import sys, urllib.request\n"
        "with urllib.request.urlopen(sys.argv[1], timeout=5) as response:\n"
        "    response.read()\n",
        encoding="utf-8",
    )
    env = {
        **child_env,
        "BROWSER": shlex.join([Path(sys.executable).as_posix(), browser.as_posix(), "%s"]),
    }
    entry = generated_commands["mcp"]["mcpServers"]["calendar"]
    with (tmp_path / "proxy-stderr.txt").open("w+", encoding="utf-8") as stderr:
        with anyio.fail_after(30):
            async with stdio_client(
                StdioServerParameters(**entry, env=env), errlog=stderr
            ) as streams:
                async with ClientSession(*streams) as client:
                    await client.initialize()
                    yield client
        stderr.seek(0)
        diagnostics = stderr.read()
        assert TOKEN not in diagnostics
        assert "Traceback" not in diagnostics


@pytest.mark.parametrize("failure", ["http-401", "auth-code"])
def test_generated_cowork_proxy_authenticates_and_replays_original_call(
    failure, upstream, generated_commands, child_env, tmp_path
):
    """Scenario: execute Cowork's generated command and call an unauthenticated MCP.

    Expected: discovery starts no browser, one sign-in succeeds, and the exact request replays
    once with its arguments intact. A later call succeeds without another sign-in.
    """
    upstream["failure"] = failure

    async def scenario():
        async with proxy_client(generated_commands, child_env, tmp_path) as client:
            assert "read_calendar" in {tool.name for tool in (await client.list_tools()).tools}
            assert upstream["browsers"] == []
            arguments = {"date": "2026-10-28", "marker": "original-request"}
            result = await client.call_tool("read_calendar", arguments)
            assert not result.model_dump(by_alias=True)["isError"]
            assert json.loads(result.content[0].text) == upstream["calls"][1]["params"]
            assert upstream["calls"][1]["params"]["arguments"] == arguments
            assert upstream["calls"][0] == upstream["calls"][1]
            assert len(upstream["calls"]) == 2
            assert len(upstream["browsers"]) == 1
            assert parse_qs(urlsplit(upstream["browsers"][0]).query) == {
                "name": [SERVICE],
                "o": ["12345"],
            }
            assert not (await client.call_tool("read_calendar", {"marker": "later"})).model_dump(
                by_alias=True
            )["isError"]
            assert len(upstream["calls"]) == 3
            assert len(upstream["browsers"]) == 1

    anyio.run(scenario)


def test_generated_proxy_discovery_exposes_explicit_authentication(
    upstream, generated_commands, child_env, tmp_path
):
    """Scenario: discovery returns -32006 before the user authenticates explicitly.

    Expected: authenticate stays callable, refresh discovers normal tools, and the user's
    subsequent task succeeds over the same stdio connection.
    """
    upstream["discovery_auth"] = True

    async def scenario():
        async with proxy_client(generated_commands, child_env, tmp_path) as client:
            assert [tool.name for tool in (await client.list_tools()).tools] == ["authenticate"]
            assert upstream["browsers"] == []
            assert not (await client.call_tool("authenticate", {})).model_dump(by_alias=True)[
                "isError"
            ]
            assert "read_calendar" in {tool.name for tool in (await client.list_tools()).tools}
            assert not (
                await client.call_tool("read_calendar", {"marker": "after-login"})
            ).model_dump(by_alias=True)["isError"]
            assert len(upstream["calls"]) == 1
            assert len(upstream["browsers"]) == 1

    anyio.run(scenario)


def test_generated_proxy_preserves_persistent_auth_failure(
    upstream, generated_commands, child_env, tmp_path
):
    """Scenario: upstream still requires authentication after a successful sign-in.

    Expected: the original error reaches the client, retries are bounded, and discovery still
    works without another browser launch.
    """
    upstream["persistent"] = True

    async def scenario():
        async with proxy_client(generated_commands, child_env, tmp_path) as client:
            await client.list_tools()
            with pytest.raises(MCPError) as error:
                await client.call_tool("read_calendar", {})
            assert error.value.error.code == -32006
            assert len(upstream["calls"]) == 2
            assert len(upstream["browsers"]) == 1
            assert "read_calendar" in {tool.name for tool in (await client.list_tools()).tools}
            assert len(upstream["browsers"]) == 1

    anyio.run(scenario)


def test_generated_proxy_preserves_permission_error_without_starting_login(
    upstream, generated_commands, child_env, tmp_path
):
    """Scenario: a tool returns an IP permission denial without an auth code or login URL.

    Expected: the proxy forwards the original error once without inferring an auth failure
    or opening a browser automatically.
    """
    upstream["failure"] = "permission"

    async def scenario():
        async with proxy_client(generated_commands, child_env, tmp_path) as client:
            await client.list_tools()
            result = await client.call_tool("read_calendar", {})
            assert result.model_dump(by_alias=True)["isError"]
            assert result.content[0].text == "Permission denied from this IP"
            assert len(upstream["calls"]) == 1
            assert upstream["browsers"] == []

    anyio.run(scenario)


def test_generated_desktop_helper_emits_token_and_fails_cleanly(
    generated_commands, child_env, tmp_path
):
    """Scenario: run the exact generated helper command with a supplied bearer, then no CLI.

    Expected: token-only stdout on success; nonzero exit, empty stdout and actionable stderr
    on failure, without opening a browser or reading the developer's credentials.
    """
    profile = generated_commands["profile"]
    command = [profile["inferenceCredentialHelper"], *profile["inferenceCredentialHelperArgs"]]
    result = subprocess.run(
        command,
        env=child_env,
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert result.stdout == TOKEN + "\n"
    assert result.stderr == ""

    no_credentials = {key: value for key, value in child_env.items() if key != "DATABRICKS_BEARER"}
    no_credentials["PATH"] = str(tmp_path / "empty-path")
    result = subprocess.run(
        command,
        env=no_credentials,
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "Databricks CLI was not found" in result.stderr
    assert "Traceback" not in result.stderr and TOKEN not in result.stderr
