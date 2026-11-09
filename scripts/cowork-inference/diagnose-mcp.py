"""Compare native connector failure with the same registration outside Desktop."""

import json
import os
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> None:
    profile_path = (
        Path(os.environ["LOCALAPPDATA"])
        / "Claude-3p/configLibrary/5b36a0b9-b56d-47cf-b6a4-0904e0bc14f6.json"
    )
    profile = json.loads(profile_path.read_text(encoding="utf-8-sig"))
    server = next(
        server
        for server in profile["managedMcpServers"]
        if "system.ai.web_search" in " ".join(server["args"])
    )
    parameters = StdioServerParameters(**server)
    parameters.env = {**os.environ, **(parameters.env or {})}
    with anyio.fail_after(60):
        async with stdio_client(parameters) as streams:
            async with ClientSession(*streams) as client:
                await client.initialize()
                names = [tool.name for tool in (await client.list_tools()).tools]
                print(json.dumps({"outsideDesktop": True, "tools": names}))


if __name__ == "__main__":
    anyio.run(main)
