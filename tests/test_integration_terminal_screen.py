"""Unit checks for the integration harness's virtual terminal screen."""

import time
from dataclasses import dataclass

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


class _Clock:
    # wait_for treats a 0.0 stability start as unset, so the clock never reads zero.
    start = 1000.0

    def __init__(self):
        self.now = self.start

    @property
    def elapsed(self):
        return self.now - self.start

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    return clock


@dataclass
class _McpReply:
    snapshot: str
    echo_delay: float = 0
    not_before: float = 0


class _ScriptedCodexPty:
    idle_poll_seconds = 0.25

    def __init__(self, clock, replies):
        self.clock = clock
        self.replies = replies
        self.pending = []
        self.typed = []

    @property
    def enters(self):
        return [at for at, keys in self.typed if keys == "\r"]

    def _reply(self):
        return self.replies[min(len(self.enters), len(self.replies) - 1)]

    def send(self, keys):
        reply = self._reply()
        self.typed.append((self.clock.elapsed, keys))
        if keys == "\r":
            chunk = "\x1b[2J\x1b[H" + reply.snapshot.replace("\n", "\r\n")
            self.pending.append((max(self.clock.elapsed, reply.not_before), chunk))
        else:
            self.pending.append((self.clock.elapsed + reply.echo_delay, f"› {keys}"))
        self.pending.sort(key=lambda item: item[0])

    def read(self, timeout):
        if self.pending and self.pending[0][0] <= self.clock.elapsed:
            return self.pending.pop(0)[1]
        self.clock.now += self.idle_poll_seconds
        return None


class _McpInventoryTerminal(AgentTerminal):
    def __init__(self, pty):
        self.agent = "codex"
        self.child = pty
        self.screen = TerminalScreen(140, 60, pty.send)
        self.stream = pyte.Stream(self.screen)
        self.output = []
        self.actions = []
        self.ended = False


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


_MCP_SERVICES = {
    "ug_e2e.tools.fixture_metadata": "describe_fixture",
    "ug_e2e.tools.fixture_reader": "read_fixture",
}
_MCP_BUDGET = 120


def _inventory_actions(terminal):
    return [action for action in terminal.actions if action["reason"] == "codex-mcp-inventory"]


@pytest.mark.parametrize("starting_snapshots", [0, 1, 3])
def test_codex_mcp_inventory_refreshes_static_startup_snapshots(clock, starting_snapshots):
    from tests.integration.utils.mcp import open_codex_mcp_inventory

    pty = _ScriptedCodexPty(
        clock,
        [_McpReply(_mcp_snapshot("starting", other_status="starting"))] * starting_snapshots
        + [_McpReply(_mcp_snapshot())],
    )
    terminal = _McpInventoryTerminal(pty)
    inventory = open_codex_mcp_inventory(terminal, _MCP_SERVICES)
    assert len(pty.enters) == starting_snapshots + 1
    assert "fixture_metadata: connected (1 tool)" in inventory
    assert "fixture_reader: connected (1 tool)" in inventory
    assert "starting" not in inventory
    assert _inventory_actions(terminal) == [{"reason": "codex-mcp-inventory", "screen": inventory}]


@pytest.mark.parametrize(
    "snapshot",
    [
        _mcp_snapshot("failed"),
        _mcp_snapshot(tool="wrong_tool"),
        _mcp_snapshot(other_status="failed"),
    ],
)
def test_codex_mcp_inventory_rejects_failed_servers_and_wrong_tools(clock, snapshot):
    from tests.integration.utils.mcp import open_codex_mcp_inventory

    pty = _ScriptedCodexPty(clock, [_McpReply(snapshot)])
    terminal = _McpInventoryTerminal(pty)
    with pytest.raises(AssertionError, match="did not connect with the expected tools"):
        open_codex_mcp_inventory(terminal, _MCP_SERVICES)
    assert len(pty.enters) == 1
    assert _inventory_actions(terminal) == []


def test_codex_mcp_inventory_bounds_permanent_startup(clock):
    from tests.integration.utils.mcp import open_codex_mcp_inventory

    pty = _ScriptedCodexPty(clock, [_McpReply(_mcp_snapshot("starting"))])
    terminal = _McpInventoryTerminal(pty)
    with pytest.raises(AssertionError, match="did not connect with the expected tools"):
        open_codex_mcp_inventory(terminal, {"ug_e2e.tools.fixture_metadata": "describe_fixture"})
    assert len(pty.enters) > 1
    assert all(at < _MCP_BUDGET for at, _ in pty.typed)
    assert clock.elapsed <= _MCP_BUDGET
    assert _inventory_actions(terminal) == []


@pytest.mark.parametrize(
    "replies",
    [
        pytest.param(
            [_McpReply(_mcp_snapshot("starting"), not_before=_MCP_BUDGET - 1)],
            id="snapshot-just-before-deadline",
        ),
        pytest.param(
            # The second submission types at 101.5s, so its echo settles at exactly 120s.
            [
                _McpReply(_mcp_snapshot("starting"), not_before=100),
                _McpReply(_mcp_snapshot(), echo_delay=18),
            ],
            id="echo-settles-at-deadline",
        ),
    ],
)
def test_codex_mcp_inventory_stops_submitting_once_the_budget_expires(clock, replies):
    from tests.integration.utils.mcp import open_codex_mcp_inventory

    pty = _ScriptedCodexPty(clock, replies)
    terminal = _McpInventoryTerminal(pty)
    with pytest.raises(AssertionError, match="did not connect with the expected tools") as error:
        open_codex_mcp_inventory(terminal, _MCP_SERVICES)
    assert "ug_e2e-tools-fixture_metadata: starting (1 tool)" in str(error.value)
    assert all(at < _MCP_BUDGET for at, _ in pty.typed)
    assert _inventory_actions(terminal) == []


@pytest.mark.parametrize("echo_delay, connects", [(10, True), (60, False)])
def test_codex_mcp_inventory_bounds_input_echo_by_the_remaining_budget(clock, echo_delay, connects):
    from tests.integration.utils.mcp import open_codex_mcp_inventory

    pty = _ScriptedCodexPty(
        clock,
        [
            _McpReply(_mcp_snapshot("starting"), not_before=100),
            _McpReply(_mcp_snapshot(), echo_delay=echo_delay),
        ],
    )
    terminal = _McpInventoryTerminal(pty)
    if connects:
        inventory = open_codex_mcp_inventory(terminal, _MCP_SERVICES)
        assert "fixture_metadata: connected (1 tool)" in inventory
    else:
        with pytest.raises(AssertionError, match="typed input") as error:
            open_codex_mcp_inventory(terminal, _MCP_SERVICES)
        assert "ug_e2e-tools-fixture_metadata: starting (1 tool)" in str(error.value)
        assert len(pty.enters) == 1
    assert clock.elapsed <= _MCP_BUDGET
