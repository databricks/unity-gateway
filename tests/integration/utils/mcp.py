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


CODEX_MCP_INVENTORY_TIMEOUT = 120
CODEX_MCP_INVENTORY_ATTEMPT_TIMEOUT = 10


def _codex_mcp_inventory_connected(tui, services: dict[str, str], timeout: float) -> None:
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
            timeout=timeout,
        )


def open_codex_mcp_inventory(tui, services: dict[str, str]) -> str:
    """Open Codex's `/mcp verbose` inventory, re-submitting while servers are still starting.

    Codex renders the inventory as a static snapshot, so a server that was still starting when it
    was printed never updates on screen. Re-submit the command until every server shows connected,
    but only while the latest snapshot still shows `starting`; any other state (failed, needs
    auth) and the overall deadline re-raise immediately.
    """
    deadline = time.monotonic() + CODEX_MCP_INVENTORY_TIMEOUT
    attempt = 0
    while True:
        attempt += 1
        output_start = len(tui.output)
        tui.submit("/mcp verbose")
        tui.actions.append({"reason": f"codex-mcp-inventory-attempt-{attempt}"})
        remaining = deadline - time.monotonic()
        try:
            _codex_mcp_inventory_connected(
                tui, services, min(CODEX_MCP_INVENTORY_ATTEMPT_TIMEOUT, max(remaining, 1))
            )
            break
        except AssertionError:
            latest = tui.visible.rsplit("MCP Tools", 1)
            still_starting = len(latest) == 1 or "starting" in latest[-1].casefold()
            if not still_starting or time.monotonic() >= deadline:
                raise
    screen = pyte.HistoryScreen(140, 60, history=1000)
    pyte.Stream(screen).feed("".join(tui.output[output_start:]))
    assert len(screen.history.top) < screen.history.size, "Codex's MCP inventory was truncated"
    history = [
        "".join(line[column].data for column in range(screen.columns))
        for line in screen.history.top
    ]
    inventory = "\n".join([*history, *screen.display])
    assert "MCP Tools" in inventory, inventory
    tui.actions.append({"reason": "codex-mcp-inventory", "screen": inventory})
    return inventory
