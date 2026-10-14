"""Managed MCP registration, connection, and tool use in both real agent TUIs."""

import hashlib
import json
import re
import uuid

import pytest

from tests.integration.utils.evidence import (
    agent_sessions,
    assistant_answers,
    is_child_session,
)
from tests.integration.utils.mcp import open_claude_mcp_inventory, open_codex_mcp_inventory

from .base import BaseCujTest
from .helpers.constants import CLAUDE, CODEX
from .helpers.mcp import registered_name
from .helpers.poll import poll
from .helpers.terminal import Terminal

CUJ_NAME = "CUJ 3 · UC MCP discovery"

pytestmark = [pytest.mark.managed, pytest.mark.mcp_registration, pytest.mark.workspace_isolated]

SERVICES = {
    "ug_e2e.tools.fixture_metadata": "describe_fixture",
    "ug_e2e.tools.fixture_reader": "read_fixture",
}


class McpFixtureTask:
    def __init__(self):
        self.run_id = uuid.uuid4().hex
        self.expected = {
            tool: hashlib.sha256(
                b"unity-gateway-cuj-fixture\0"
                + tool.encode("ascii")
                + b"\0"
                + self.run_id.encode("ascii")
            ).hexdigest()
            for tool in SERVICES.values()
        }
        calls = "; ".join(
            f"call {tool} on {registered_name(service)} with run_id={self.run_id}"
            for service, tool in SERVICES.items()
        )
        keys = " and ".join(f'"{tool}"' for tool in SERVICES.values())
        self.prompt = (
            f"Use these read-only MCP tools: {calls}. "
            "Do not use shell commands, files, or subagents. Reply with only a JSON object "
            f"whose keys are exactly the bare tool names {keys}, with no server name or "
            "mcp__ prefix, each mapped to the text that tool returned, without code fences."
        )
        # Claude may key a receipt by its native tool name and echo the raw tool result.
        self.aliases = {
            f"mcp__{registered_name(service)}__{tool}": tool for service, tool in SERVICES.items()
        }

    def _receipts(self, observed):
        if not isinstance(observed, dict):
            return None
        receipts = {}
        for key, value in observed.items():
            tool = self.aliases.get(key, key)
            if isinstance(value, str) and value.startswith("{"):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    return None
            if isinstance(value, dict) and value.keys() == {"result"}:
                value = value["result"]
            if tool in receipts:
                return None
            receipts[tool] = value
        return receipts

    def answers(self, session, agent: str) -> list[str]:
        return [
            answer
            for path, records in agent_sessions(session, agent).items()
            if not is_child_session(agent, path, records)
            for answer in assistant_answers(agent, records)
        ]

    def completed(self, session, agent: str) -> bool:
        for answer in self.answers(session, agent):
            # The TUI renders a fenced reply without its fence.
            fenced = re.fullmatch(r"\s*```(?:json)?\n(.*)\n```\s*", answer, re.DOTALL)
            try:
                observed = json.loads(fenced.group(1) if fenced else answer)
            except json.JSONDecodeError:
                continue
            if self._receipts(observed) == self.expected:
                return True
        return False


def _wait_for_mcp_task(tui, task):
    approved_calls = set()
    expected_tools = {(registered_name(service), tool) for service, tool in SERVICES.items()}

    def approve_expected_codex_call(screen):
        if not (
            "Allow for this session" in screen
            and "Always allow" in screen
            and "Cancel" in screen
            and re.search(r"(?m)^[ \t]*[›❯>][ \t]*[1-4]\.", screen)
        ):
            return False
        questions = list(re.finditer(r"(?m)^[ \t]*(Allow\b[^\n?]+\?)[ \t]*$", screen))
        assert questions, "Unexpected approval menu; inspect the actual command:\n" + screen
        question = questions[-1].group(1)
        approval = re.fullmatch(
            r'Allow the (?P<server>\S+) MCP server to run tool "(?P<tool>[^"]+)"\?',
            question,
        )
        assert tui.agent == CODEX and approval, (
            "Unexpected Codex permission request; inspect the actual command:\n" + screen
        )
        tail = screen[questions[-1].end() :]
        run_match = re.search(r"(?m)^[ \t]*run_id:[ \t]*(\S+)[ \t]*$", tail)
        selected = re.search(
            r"(?m)^[ \t]*[›❯>][ \t]*([1-4])\.[ \t]*(Allow(?: for this session)?|Always allow|Cancel)\b",
            tail,
        )
        assert run_match and selected, (
            "Unexpected Codex approval menu; inspect the actual command:\n" + screen
        )
        server = approval.group("server")
        tool = approval.group("tool")
        run_id = run_match.group(1)
        assert (server, tool) in expected_tools and run_id == task.run_id, (
            "Unexpected MCP permission request; inspect the actual command:\n" + screen
        )
        assert selected.group(1) == "1" and selected.group(2) == "Allow", (
            "Codex MCP approval must remain a per-call Allow; inspect the actual menu:\n" + screen
        )
        call = (server, tool, run_id)
        if call in approved_calls:
            return False
        tui.send("\r", f"allow one MCP call: {server}.{tool}")
        approved_calls.add(call)
        return True

    try:
        tui.wait_until(
            lambda: task.completed(tui.session, tui.agent),
            "a completed assistant answer with both verified MCP receipts",
            rejected=("Do you want to proceed?", "Do you want to allow"),
            on_screen=approve_expected_codex_call,
        )
    except AssertionError as error:
        answers = task.answers(tui.session, tui.agent)
        raise AssertionError(f"{error}\nNative assistant answers: {answers!r}") from None


def _fixture_server_rows(output):
    return {
        service: [
            " ".join(line.split())
            for line in output.splitlines()
            if line.split()[:1] == [registered_name(service)]
        ]
        for service in SERVICES
    }


def _expected_fixture_server_rows():
    return {
        service: [f"{registered_name(service)} {service} (managed) {CLAUDE}, {CODEX} connected"]
        for service in SERVICES
    }


def _poll_mcp_list(session):
    """Return the first `ug mcp list` output whose fixture rows match, else the last one.

    Claude's single-shot health probe can report a cold-starting server as failed.
    """

    def listing():
        result = session.run("mcp", "list", timeout=120)
        return f"{result.stdout}\n{result.stderr}"

    return poll(
        listing,
        timeout=90,
        interval=5,
        done=lambda output: _fixture_server_rows(output) == _expected_fixture_server_rows(),
    )


@pytest.fixture(autouse=True)
def _revert_mcp_test_state(cuj):
    yield
    session, _, _ = cuj
    session.revert_machine_wide(
        "mcp-registration-cleanup-revert",
        "MCP registration CUJ teardown left machine-wide agent settings",
    )


class TestMcpRegistration(BaseCujTest):
    WORKSPACE_URL = "https://dbc-bbdd5508-648e.cloud.databricks.com"

    def test_mcp_list_reports_managed_fixture_servers(self, cuj):
        """Scenario: configure both agents and inspect unscoped `ug mcp list` output.

        Expected: both managed fixture rows list Claude/Codex and aggregate `connected` status;
        no `ug_e2e.other_tools` server appears. Codex connectivity is verified by its TUI journey.
        """
        session, workspace, _recorder = cuj
        session.configure(
            [
                "configure",
                "--agents",
                f"{CLAUDE},{CODEX}",
                "--workspace",
                workspace.url,
                "--skip-upgrade",
            ]
        )
        output = _poll_mcp_list(session)
        assert _fixture_server_rows(output) == _expected_fixture_server_rows(), output
        assert "ug_e2e.other_tools" not in output, output
        assert "ug_e2e-other_tools" not in output, output

    @pytest.mark.claude
    @pytest.mark.tui
    def test_mcp_registration_claude_servers_connect_and_work(self, cuj):
        """Scenario: configure UG, inspect Claude's MCP menu, then call both fixture tools.

        Expected: both scoped servers are registered, connected, and expose their tools;
        no ug_e2e.other_tools server or decoy tool appears in the complete inventory;
        a parent assistant answer contains the correct receipts for a fresh run ID.
        """
        session, workspace, _recorder = cuj
        session.configure(["configure", "--workspace", workspace.url, "--skip-upgrade"])
        task = McpFixtureTask()
        allowed_tools = ",".join(
            f"mcp__{registered_name(service)}__{tool}" for service, tool in SERVICES.items()
        )
        with Terminal(
            session,
            "mcp-registration-claude",
            [CLAUDE, "--", "--allowedTools", allowed_tools],
        ) as tui:
            tui.boot()
            inventory = open_claude_mcp_inventory(tui, SERVICES)
            assert "ug_e2e-other_tools" not in inventory, inventory
            assert "decoy_status" not in inventory, inventory
            tui.submit(task.prompt)
            _wait_for_mcp_task(tui, task)
            tui.exit_normally()
        assert task.completed(session, CLAUDE)
        session.assert_not_routed()

    @pytest.mark.codex
    @pytest.mark.tui
    def test_mcp_registration_codex_servers_connect_and_work(self, cuj):
        """Scenario: configure UG, inspect Codex's MCP inventory, then call both fixture tools.

        Expected: both scoped servers are registered, connected, and expose their tools;
        no ug_e2e.other_tools server or decoy tool appears in the complete inventory;
        a completed parent turn returns the correct receipts for a fresh run ID.
        """
        session, workspace, _recorder = cuj
        session.configure(["configure", "--workspace", workspace.url, "--skip-upgrade"])
        task = McpFixtureTask()
        with Terminal(session, "mcp-registration-codex", [CODEX]) as tui:
            tui.boot()
            inventory = open_codex_mcp_inventory(tui, SERVICES)
            assert "ug_e2e-other_tools" not in inventory, inventory
            assert "decoy_status" not in inventory, inventory
            tui.submit(task.prompt)
            _wait_for_mcp_task(tui, task)
            tui.exit_normally()
        assert task.completed(session, CODEX)
        session.assert_not_routed()
