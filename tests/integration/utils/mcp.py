"""Inspect MCP inventories through the real agent terminals."""

import re


def open_claude_mcp_inventory(tui, services: dict[str, str]) -> None:
    tui.submit("/mcp")
    tui.wait_for(
        lambda screen: "Manage MCP servers" in screen,
        "Claude's MCP menu",
        timeout=120,
    )
    for service, tool in services.items():
        name = service.replace(".", "-")
        tui.choose("Manage MCP servers", name)
        tui.wait_for(
            lambda screen, name=name: (
                name in screen
                and re.search(r"(?m)^\s*Status:\s*(?:[✓✔]\s*)?connected\s*$", screen)
                and "View tools" in screen
                and "tools fetch failed" not in screen
            ),
            f"{name} connected with tools loaded",
            timeout=120,
        )
        tui.actions.append({"reason": f"mcp-connected-{name}", "screen": tui.visible})
        tui.choose("MCP Server", "View tools")
        tui.wait_for(
            lambda screen, tool=tool: re.search(rf"\b{re.escape(tool)}\b", screen),
            f"{tool} in {name}'s tools menu",
        )
        tui.actions.append({"reason": f"mcp-tools-{name}", "screen": tui.visible})
        tui.send("\x1b", "return from the MCP tools view")
        tui.wait_for(
            lambda screen: "MCP Server" in screen or "Manage MCP servers" in screen,
            "the MCP server detail or inventory",
        )
        if "Manage MCP servers" not in tui.visible:
            tui.send("\x1b", "return to the MCP inventory")
            tui.wait_for(
                lambda screen: "Manage MCP servers" in screen,
                "Claude's MCP inventory",
            )
    tui.send("\x1b", "close Claude's MCP menu")
    tui.wait_for(
        lambda screen: (
            "Manage MCP servers" not in screen and re.search(r"(?m)^\s*[❯›>]\s*(?!\d+[.)])", screen)
        ),
        "Claude's prompt after closing the MCP menu",
    )


def open_codex_mcp_inventory(tui, services: dict[str, str]) -> None:
    tui.submit("/mcp verbose")
    for service, tool in services.items():
        name = re.escape(service.replace(".", "-"))
        tui.wait_for(
            lambda screen, name=name, tool=tool: re.search(
                rf"(?m)^\s*• {name}: connected \(1 tool\)\s*\n"
                rf"\s*• Auth: [^\n]*\n"
                rf"\s*• Tools: {re.escape(tool)}\s*$",
                screen,
            ),
            f"{service} connected with {tool} loaded in Codex's MCP inventory",
            timeout=120,
        )
    tui.actions.append({"reason": "codex-mcp-inventory", "screen": tui.visible})
