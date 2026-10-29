"""Native Desktop input paths and installed Cowork plugin evidence."""

import json
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

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


async def assert_desktop_mcp(session, registration: dict, workspace: str) -> None:
    assert registration["type"] == "stdio", registration
    arguments = registration["args"]
    assert arguments[arguments.index("--url") + 1] == (
        workspace + "/ai-gateway/mcp-services/system.ai.web_search"
    ), registration
    parameters = StdioServerParameters(**registration)
    parameters.env = {**session.env, **(parameters.env or {})}
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as diagnostics:
        try:
            with anyio.fail_after(60):
                async with stdio_client(parameters, errlog=diagnostics) as streams:
                    async with ClientSession(*streams) as client:
                        await client.initialize()
                        tools = {tool.name for tool in (await client.list_tools()).tools}
                        assert "web_search" in tools, tools
                        result = await client.call_tool(
                            "web_search", {"query": "Databricks official website"}
                        )
                        # MCP 1.x and 2.x expose different Python attribute names.
                        response = result.model_dump(by_alias=True)
                        session.record("desktop-mcp.json", response)
                        assert not response["isError"], session.redact(json.dumps(response))
                        text = "\n".join(
                            block["text"]
                            for block in response["content"]
                            if block["type"] == "text"
                        )
                        assert any(
                            urlparse(url).hostname in {"databricks.com", "www.databricks.com"}
                            for url in re.findall(r"https?://[^\s)<>]+", text)
                        ), session.redact(text)
        finally:
            diagnostics.seek(0)
            stderr = session.redact(diagnostics.read())
            session.record("desktop-mcp-stderr.json", {"stderr": stderr})
        assert "Traceback" not in stderr, stderr
