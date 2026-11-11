"""Component checks for polling Codex's printed MCP inventory snapshots."""

from types import SimpleNamespace

import pytest

from tests.integration.utils import mcp


@pytest.mark.parametrize(
    "snapshots,succeeds",
    [
        ([("connected", "connected")], True),
        ([("starting", "connected"), ("connected", "connected")], True),
        ([("starting", "connected")], False),
        ([("connected", "starting"), ("starting", "connected")], False),
    ],
)
def test_codex_inventory_requires_both_servers_connected_in_same_snapshot(
    monkeypatch, snapshots, succeeds
):
    clock = [0]
    monkeypatch.setattr(mcp, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    services = {"fixture.metadata": "describe_fixture", "fixture.reader": "read_fixture"}

    # The terminal boundary prints a fresh inventory only when /mcp is submitted.
    class Terminal:
        def __init__(self):
            self.output = []
            self.actions = []
            self.snapshots = iter(snapshots)
            self.visible = ""

        def submit(self, command):
            assert command == "/mcp verbose"
            statuses = next(self.snapshots, None)
            if statuses is None:
                return
            rows = ["MCP Tools"]
            for (service, tool), status in zip(services.items(), statuses, strict=True):
                rows.extend(
                    [
                        f"  • {service.replace('.', '-')}: {status} (1 tool)",
                        "    • Auth: Unsupported",
                        f"    • Tools: {tool}",
                    ]
                )
            snapshot = "\n".join(rows)
            self.visible += "\n" + snapshot
            self.output.append("\r\n".join(rows) + "\r\n")

        def wait_for(self, predicate, description, timeout):
            for clock[0] in range(timeout):
                if predicate(self.visible):
                    return
            raise AssertionError(description)

    terminal = Terminal()
    if succeeds:
        inventory = mcp.open_codex_mcp_inventory(terminal, services)
        latest = inventory.rsplit("MCP Tools", 1)[-1]
        assert "fixture-metadata: connected" in latest
        assert "fixture-reader: connected" in latest
        assert "starting" not in latest
    else:
        with pytest.raises(AssertionError, match="all expected Codex MCP servers connected"):
            mcp.open_codex_mcp_inventory(terminal, services)
