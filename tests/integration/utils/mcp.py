"""Inspect MCP inventories through the real agent terminals."""

import re
import time

import pyte


def _claude_mcp_row_key(line: str) -> str:
    """Identify the selected server independently of its spinner/status/tool count."""
    match = re.search(r"[\w][\w.-]*", line)
    return match.group() if match else ""


def open_claude_mcp_inventory(tui, services: dict[str, str]) -> str:
    tui.submit("/mcp")
    tui.wait_for(
        lambda screen: "Manage MCP servers" in screen and tui.selected_line(),
        "Claude's MCP menu",
        timeout=120,
    )
    tui.wait_for(
        lambda screen: "connecting" not in screen.casefold(),
        "Claude's MCP startup checks to finish before opening server details",
        timeout=120,
    )
    first = _claude_mcp_row_key(tui.selected_line())
    screens = []
    for _ in range(100):
        screens.append(tui.visible)
        current = _claude_mcp_row_key(tui.selected_line())
        tui.send("\x1b[B", "inspect the next MCP inventory row")
        tui.wait_for(
            lambda screen, current=current: _claude_mcp_row_key(tui.selected_line()) != current,
            "the next MCP inventory row",
        )
        if _claude_mcp_row_key(tui.selected_line()) == first:
            break
    else:
        raise AssertionError("Claude's MCP inventory did not wrap within 100 rows")
    inventory = "\n".join(screens)
    tui.actions.append({"reason": "claude-mcp-inventory", "screen": inventory})
    for service, tool in services.items():
        name = service.replace(".", "-")
        tui.choose("Manage MCP servers", name)
        tui.wait_for(
            lambda screen, name=name: (
                name in screen.casefold()
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
    return inventory


def open_codex_mcp_inventory(tui, services: dict[str, str]) -> str:
    deadline = time.monotonic() + 120
    current = ""

    def remaining():
        left = deadline - time.monotonic()
        assert left > 0, (
            f"Codex's MCP servers did not connect with the expected tools within 120s:\n{current}"
        )
        return left

    def inventory():
        screen = pyte.HistoryScreen(140, 60, history=1000)
        pyte.Stream(screen).feed("".join(tui.output[output_start:]))
        assert len(screen.history.top) < screen.history.size, "Codex's MCP inventory was truncated"
        history = [
            "".join(line[column].data for column in range(screen.columns))
            for line in screen.history.top
        ]
        return "\n".join([*history, *screen.display])

    while True:
        remaining()
        output_start = len(tui.output)
        tui.send("/mcp verbose", "type text before pressing Enter")
        tui.wait_for(
            lambda screen: "/mcpverbose" in "".join(screen.split()),
            "typed input",
            timeout=remaining(),
            stable_for=0.5,
        )
        remaining()
        tui.send("\r", "press Enter after the input has rendered")
        tui.wait_for(
            lambda _: re.search(r"(?ms)MCP Tools.*?^\s*[›❯>]\s", inventory()),
            "Codex's complete MCP inventory response",
            timeout=remaining(),
        )
        current = inventory()
        pending = [
            service
            for service, tool in services.items()
            if not re.search(
                rf"(?m)^\s*• {re.escape(service.replace('.', '-'))}: connected \(1 tool\)\s*\n"
                rf"\s*• Auth: [^\n]*\n"
                rf"\s*• Tools: {re.escape(tool)}\s*$",
                current,
            )
        ]
        if not pending:
            tui.actions.append({"reason": "codex-mcp-inventory", "screen": current})
            return current
        assert all(
            re.search(rf"(?m)^\s*• {re.escape(service.replace('.', '-'))}: starting\b", current)
            for service in pending
        ), f"Codex's MCP servers did not connect with the expected tools:\n{current}"
        # /mcp prints a snapshot; startup completion does not update its old rows.
        time.sleep(min(1, remaining()))
