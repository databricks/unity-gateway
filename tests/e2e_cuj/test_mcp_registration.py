"""Managed MCP registration, connection, and tool use in both real agent TUIs."""

import hashlib
import json
import os
import uuid

import pytest
from utils.evidence import (
    agent_sessions,
    assert_no_terminal_api_error,
    assistant_answers,
    is_child_session,
)
from utils.mcp import open_claude_mcp_inventory, open_codex_mcp_inventory
from utils.terminal import AgentTerminal

from .base import BaseCujTest

pytestmark = [pytest.mark.managed, pytest.mark.mcp_registration, pytest.mark.workspace_isolated]

SERVICES = {
    "ug_e2e.tools.fixture_metadata": "describe_fixture",
    "ug_e2e.tools.fixture_reader": "read_fixture",
}


class McpFixtureTask:
    def __init__(self):
        run_id = uuid.uuid4().hex
        self.expected = {
            tool: hashlib.sha256(
                b"unity-gateway-cuj-fixture\0"
                + tool.encode("ascii")
                + b"\0"
                + run_id.encode("ascii")
            ).hexdigest()
            for tool in SERVICES.values()
        }
        calls = "; ".join(
            f"call {tool} on {service.replace('.', '-')} with run_id={run_id}"
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
                if observed == self.expected:
                    return True
        return False


def _wait_for_mcp_task(tui, task):
    def completed(screen):
        assert_no_terminal_api_error(screen)
        return task.completed(tui.session, tui.agent)

    tui.wait_for(
        completed,
        "a completed assistant answer with both verified MCP receipts",
        timeout=240,
    )


class TestMcpRegistration(BaseCujTest):
    WORKSPACE_URL = os.environ.get("UCODE_TEST_WORKSPACE", "")

    @pytest.mark.claude
    @pytest.mark.tui
    def test_mcp_registration_claude_servers_connect_and_work(self, live_session, workspace):
        """Scenario: configure UG, inspect Claude's MCP menu, then call both fixture tools.

        Expected: both scoped servers are registered, connected, and expose their tools;
        no ug_e2e.other_tools server or decoy tool appears in the complete inventory;
        a parent assistant answer contains the correct receipts for a fresh run ID.
        """
        session = live_session
        session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=300)
        task = McpFixtureTask()
        allowed_tools = ",".join(
            f"mcp__{service.replace('.', '-')}__{tool}" for service, tool in SERVICES.items()
        )
        with AgentTerminal(
            session,
            "claude",
            [str(session.binary), "claude", "--", "--allowedTools", allowed_tools],
            "mcp-registration-claude",
        ) as tui:
            tui.boot()
            inventory = open_claude_mcp_inventory(tui, SERVICES)
            assert "ug_e2e-other_tools" not in inventory, inventory
            assert "decoy_status" not in inventory, inventory
            tui.submit(task.prompt)
            _wait_for_mcp_task(tui, task)
            tui.exit_normally()
        assert task.completed(session, "claude")
        session.assert_not_routed()

    @pytest.mark.codex
    @pytest.mark.tui
    def test_mcp_registration_codex_servers_connect_and_work(self, live_session, workspace):
        """Scenario: configure UG, inspect Codex's MCP inventory, then call both fixture tools.

        Expected: both scoped servers are registered, connected, and expose their tools;
        no ug_e2e.other_tools server or decoy tool appears in the complete inventory;
        a completed parent turn returns the correct receipts for a fresh run ID.
        """
        session = live_session
        session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=300)
        task = McpFixtureTask()
        with AgentTerminal(
            session, "codex", [str(session.binary), "codex"], "mcp-registration-codex"
        ) as tui:
            tui.boot()
            inventory = open_codex_mcp_inventory(tui, SERVICES)
            assert "ug_e2e-other_tools" not in inventory, inventory
            assert "decoy_status" not in inventory, inventory
            tui.submit(task.prompt)
            _wait_for_mcp_task(tui, task)
            tui.exit_normally()
        assert task.completed(session, "codex")
        session.assert_not_routed()
