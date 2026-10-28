"""Real SDK calls to the read-only MCP receipt fixtures."""

import hashlib
import json
import tempfile

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

MCP_FIXTURE_SERVICES = {
    "ug_e2e.tools.fixture_metadata": "describe_fixture",
    "ug_e2e.tools.fixture_reader": "read_fixture",
}


def fixture_receipt(tool: str, run_id: str) -> str:
    return hashlib.sha256(
        b"unity-gateway-cuj-fixture\0" + tool.encode("ascii") + b"\0" + run_id.encode("ascii")
    ).hexdigest()


async def assert_mcp_fixture_call(session, registration: dict, tool: str, run_id: str) -> None:
    parameters = StdioServerParameters(**registration)
    parameters.env = {**session.env, **(parameters.env or {})}
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as diagnostics:
        try:
            with anyio.fail_after(60):
                async with stdio_client(parameters, errlog=diagnostics) as streams:
                    async with ClientSession(*streams) as client:
                        await client.initialize()
                        assert tool in {item.name for item in (await client.list_tools()).tools}
                        result = (await client.call_tool(tool, {"run_id": run_id})).model_dump(
                            by_alias=True
                        )
                        assert not result["isError"], result
                        assert fixture_receipt(tool, run_id) in json.dumps(result["content"]), (
                            result
                        )
        finally:
            diagnostics.seek(0)
            stderr = session.redact(diagnostics.read())
            session.record(f"proxy-{tool}-stderr.json", {"stderr": stderr})
        assert "Traceback" not in stderr
    session.record(f"proxy-{tool}.json", {"run_id": run_id, "result": result})
