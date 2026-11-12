"""Unit checks for the integration harness's virtual terminal screen."""

import pyte
import pytest

from tests.integration.utils.terminal import AgentTerminal, TerminalScreen


def _screen() -> TerminalScreen:
    return TerminalScreen(10, 2, lambda data: None)


def test_display_renders_text_like_pyte():
    screen = _screen()
    pyte.Stream(screen).feed("hi 世界")

    assert screen.display[0] == "hi 世界" + " " * 3
    assert screen.display == pyte.Screen.display.fget(screen)


def test_display_treats_an_empty_cell_as_a_space():
    screen = _screen()
    pyte.Stream(screen).feed("ab")
    screen.buffer[0][1] = screen.buffer[0][1]._replace(data="")

    assert screen.display[0] == "a" + " " * 9


class _TaskWaitTerminal:
    wait_for_task = AgentTerminal.wait_for_task

    def __init__(self, session, screens):
        self.agent = "claude"
        self.session = session
        self.screens = screens
        self.sent = []

    def approve_windows_command(self, screen):
        return False

    def send(self, keys, reason):
        self.sent.append((keys, reason))

    def wait_for(self, predicate, description, timeout):
        assert not any(predicate(screen) for screen in self.screens[:-1])
        assert predicate(self.screens[-1]), description


class _Task:
    filename = "input.txt"

    def __init__(self):
        self.asserted = False

    def completed(self, session, agent):
        return True

    def assert_completed(self, session, agent):
        self.asserted = True


def test_wait_for_task_custom_evidence_keeps_permission_handling(tmp_path):
    session = type("Session", (), {"cwd": tmp_path / "project"})()
    dialog = f'Do you want to proceed?\nfind {tmp_path} -name "input.txt" 2>/dev/null\n❯ 1. Yes'
    terminal = _TaskWaitTerminal(session, [dialog, "ready"])
    task = _Task()
    terminal.wait_for_task(task, completion=lambda: True)
    assert terminal.sent == [("\r", "allow read-only search for input.txt")]
    assert not task.asserted


def test_wait_for_task_default_still_asserts_parent_answer():
    terminal = _TaskWaitTerminal(None, ["ready"])
    task = _Task()
    terminal.wait_for_task(task)
    assert task.asserted


def test_wait_for_task_custom_evidence_does_not_hide_api_errors():
    terminal = _TaskWaitTerminal(None, ["unexpected status 401"])
    with pytest.raises(AssertionError, match="terminal API error"):
        terminal.wait_for_task(_Task(), completion=lambda: True)


class _McpInventoryTerminal:
    """Control only terminal output; exercise the real inventory reader and parser."""

    def __init__(self, snapshots):
        self.snapshots = snapshots
        self.output = []
        self.actions = []
        self.queries = 0

    def submit(self, text):
        assert text == "/mcp verbose"
        index = min(self.queries, len(self.snapshots) - 1)
        self.queries += 1
        self.output.append("\x1b[2J\x1b[H" + self.snapshots[index].replace("\n", "\r\n"))

    def wait_for(self, predicate, description, timeout):
        assert timeout > 0
        assert predicate(self.snapshots[min(self.queries - 1, len(self.snapshots) - 1)]), (
            description
        )


def _mcp_snapshot(status="connected", tool="describe_fixture", other_status="connected"):
    return (
        "🔌 MCP Tools\n\n"
        f"  • ug_e2e-tools-fixture_metadata: {status} (1 tool)\n"
        "    • Auth: Unsupported\n"
        f"    • Tools: {tool}\n"
        "    • Resources: (none)\n"
        "    • Resource templates: (none)\n\n"
        f"  • ug_e2e-tools-fixture_reader: {other_status} (1 tool)\n"
        "    • Auth: Unsupported\n"
        "    • Tools: read_fixture\n"
        "    • Resources: (none)\n"
        "    • Resource templates: (none)\n\n"
        "› Ask Codex to do anything\n"
    )


@pytest.mark.parametrize("starting_snapshots", [0, 1, 3])
def test_codex_mcp_inventory_refreshes_static_startup_snapshots(monkeypatch, starting_snapshots):
    from tests.integration.utils import mcp

    monkeypatch.setattr(mcp.time, "sleep", lambda _: None)
    terminal = _McpInventoryTerminal(
        [_mcp_snapshot("starting", other_status="starting")] * starting_snapshots
        + [_mcp_snapshot()]
    )
    inventory = mcp.open_codex_mcp_inventory(
        terminal,
        {
            "ug_e2e.tools.fixture_metadata": "describe_fixture",
            "ug_e2e.tools.fixture_reader": "read_fixture",
        },
    )
    assert terminal.queries == starting_snapshots + 1
    assert "fixture_metadata: connected (1 tool)" in inventory
    assert "fixture_reader: connected (1 tool)" in inventory
    assert "starting" not in inventory
    assert terminal.actions == [{"reason": "codex-mcp-inventory", "screen": inventory}]


@pytest.mark.parametrize(
    "snapshot",
    [
        _mcp_snapshot("failed"),
        _mcp_snapshot(tool="wrong_tool"),
        _mcp_snapshot(other_status="failed"),
    ],
)
def test_codex_mcp_inventory_rejects_failed_servers_and_wrong_tools(snapshot):
    from tests.integration.utils.mcp import open_codex_mcp_inventory

    terminal = _McpInventoryTerminal([snapshot])
    with pytest.raises(AssertionError, match="did not connect with the expected tools"):
        open_codex_mcp_inventory(
            terminal,
            {
                "ug_e2e.tools.fixture_metadata": "describe_fixture",
                "ug_e2e.tools.fixture_reader": "read_fixture",
            },
        )
    assert terminal.queries == 1
    assert terminal.actions == []


def test_codex_mcp_inventory_bounds_permanent_startup(monkeypatch):
    from tests.integration.utils import mcp

    clock = iter(range(0, 1000, 10))
    monkeypatch.setattr(mcp.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(mcp.time, "sleep", lambda _: None)
    terminal = _McpInventoryTerminal([_mcp_snapshot("starting")])
    with pytest.raises(AssertionError, match="did not connect with the expected tools"):
        mcp.open_codex_mcp_inventory(
            terminal, {"ug_e2e.tools.fixture_metadata": "describe_fixture"}
        )
    assert terminal.queries == 6
    assert terminal.actions == []
