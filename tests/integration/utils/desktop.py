"""Native Desktop input paths and installed Cowork plugin evidence."""

import json
import os
import sys
import tempfile
from hashlib import sha256
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def desktop_config_library(session) -> Path:
    if os.name == "nt":
        return Path(session.env["LOCALAPPDATA"]) / "Claude-3p/configLibrary"
    if sys.platform == "darwin":
        return session.home / "Library/Application Support/Claude-3p/configLibrary"
    return Path(session.env["XDG_CONFIG_HOME"]) / "Claude-3p/configLibrary"


def prepare_cowork_home(session) -> Path:
    # Pre-existing Desktop account directories are scenario inputs, not UG-generated state.
    root = desktop_config_library(session).parent / "local-agent-mode-sessions" / "account" / "org"
    root.mkdir(parents=True)
    return root


def installed_cowork_servers(session, root: Path) -> dict:
    state = json.loads((session.home / ".ucode/claude-cowork.json").read_text())
    assert Path(state["cowork_root"]) == root
    installed = json.loads((root / "cowork_plugins/installed_plugins.json").read_text())
    entries = installed["plugins"]["ug@unity-gateway-cowork"]
    assert len(entries) == 1, entries
    plugin = Path(entries[0]["installPath"])
    manifest = json.loads((plugin / ".claude-plugin/plugin.json").read_text())
    assert manifest["name"] == "ug"
    assert manifest["mcpServers"] == "./.mcp.json"
    return json.loads((plugin / manifest["mcpServers"]).read_text())["mcpServers"]


async def assert_desktop_mcp(session, registration: dict, workspace: str, run_id: str) -> None:
    assert registration["type"] == "stdio", registration
    arguments = registration["args"]
    assert arguments[arguments.index("--url") + 1] == (
        workspace + "/ai-gateway/mcp-services/system.ai.dbsql"
    ), registration
    parameters = StdioServerParameters(**registration)
    parameters.env = {**session.env, **(parameters.env or {})}
    responses = []
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as diagnostics:
        try:
            with anyio.fail_after(180):
                async with stdio_client(parameters, errlog=diagnostics) as streams:
                    async with ClientSession(*streams) as client:
                        await client.initialize()
                        tools = {tool.name for tool in (await client.list_tools()).tools}
                        assert {"execute_sql", "poll_sql_result"} <= tools, tools

                        async def call(name, arguments):
                            result = await client.call_tool(name, arguments)
                            responses.append(result.model_dump(by_alias=True))
                            session.record("desktop-mcp.json", responses)
                            assert not result.isError, session.redact(json.dumps(responses[-1]))
                            blocks = [
                                block.text for block in result.content if block.type == "text"
                            ]
                            assert len(blocks) == 1, responses[-1]
                            return json.loads(blocks[0])

                        result = await call(
                            "execute_sql",
                            {"query": f"SELECT sha2('{run_id}', 256) AS desktop_mcp_receipt"},
                        )
                        while result["status"]["state"] in {"PENDING", "RUNNING"}:
                            await anyio.sleep(1)
                            result = await call(
                                "poll_sql_result", {"statement_id": result["statement_id"]}
                            )
                        assert result["status"]["state"] == "SUCCEEDED", result
                        assert result["manifest"]["total_row_count"] == 1, result
                        # A computed receipt proves execution rather than an echoed SQL request.
                        assert sha256(run_id.encode()).hexdigest() in json.dumps(result["result"])
        finally:
            diagnostics.seek(0)
            stderr = session.redact(diagnostics.read())
            session.record("desktop-mcp-stderr.json", {"stderr": stderr})
        assert "Traceback" not in stderr, stderr
