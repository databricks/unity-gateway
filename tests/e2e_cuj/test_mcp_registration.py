"""Managed MCP registration, connection, and tool use in both real agent TUIs."""

import hashlib
import json
import re
import uuid

import pytest

from tests.integration.utils.evidence import (
    agent_sessions,
    assert_no_terminal_api_error,
    assistant_answers,
    is_child_session,
)
from tests.integration.utils.mcp import open_claude_mcp_inventory, open_codex_mcp_inventory
from tests.integration.utils.terminal import TerminalProcess

from .base import BaseCujTest
from .helpers.constants import CLAUDE, CODEX, MANAGED_PATHS
from .helpers.terminal import Terminal

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
            f"call {tool} on {service.replace('.', '-')} with run_id={self.run_id}"
            for service, tool in SERVICES.items()
        )
        self.prompt = (
            f"Use these read-only MCP tools: {calls}. "
            "Do not use shell commands, files, or subagents. Reply with only a JSON object "
            "mapping each tool name to the text it returned, without code fences."
        )

    def completed(self, session, agent: str) -> bool:
        for path, records in agent_sessions(session, agent).items():
            if is_child_session(agent, path, records):
                continue
            for answer in assistant_answers(agent, records):
                try:
                    observed = json.loads(answer)
                except json.JSONDecodeError:
                    continue
                if not isinstance(observed, dict):
                    continue
                # Claude may key answers by its qualified `mcp__<server>__<tool>` name.
                tools = {str(key).rpartition("__")[2]: value for key, value in observed.items()}
                if tools == self.expected:
                    return True
        return False


def _wait_for_mcp_task(tui, task):
    approved_calls = set()
    expected_tools = {(service.replace(".", "-"), tool) for service, tool in SERVICES.items()}

    def completed(screen):
        assert_no_terminal_api_error(screen)
        assert not any(
            prompt in screen for prompt in ("Do you want to proceed?", "Do you want to allow")
        ), "Unexpected permission request; inspect the actual command:\n" + screen

        if (
            "Allow for this session" in screen
            and "Always allow" in screen
            and "Cancel" in screen
            and re.search(r"(?m)^[ \t]*[›❯>][ \t]*[1-4]\.", screen)
        ):
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
                "Codex MCP approval must remain a per-call Allow; inspect the actual menu:\n"
                + screen
            )
            call = (server, tool, run_id)
            if call not in approved_calls:
                tui.send("\r", f"allow one MCP call: {server}.{tool}")
                approved_calls.add(call)
                return False
        return task.completed(tui.session, tui.agent)

    tui.wait_for(
        completed,
        "a completed assistant answer with both verified MCP receipts",
        timeout=240,
    )


@pytest.fixture(autouse=True)
def _revert_mcp_test_state(cuj):
    yield
    session, _, _ = cuj
    state_dir = session.home / ".ucode"
    if any(
        (state_dir / name).is_file() for name in ("state.json", "managed-backups/manifest.json")
    ):
        with TerminalProcess(
            session,
            "ug",
            [str(session.binary), "revert"],
            "mcp-registration-cleanup-revert",
        ) as terminal:
            terminal.finish()
    assert not any(path.exists() for path in MANAGED_PATHS), (
        "MCP registration CUJ teardown left machine-wide agent settings"
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
        listing = session.run("mcp", "list", timeout=120)
        output = f"{listing.stdout}\n{listing.stderr}"
        for service in SERVICES:
            server_name = service.replace(".", "-")
            rows = [
                " ".join(line.split())
                for line in output.splitlines()
                if line.split()[:1] == [server_name]
            ]
            expected_row = f"{server_name} {service} (managed) {CLAUDE}, {CODEX} connected"
            assert rows == [expected_row], output
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
            f"mcp__{service.replace('.', '-')}__{tool}" for service, tool in SERVICES.items()
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
