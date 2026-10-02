"""CUJ: switch workspaces after registering the Databricks skills MCP connection."""

import os
from urllib.parse import urlparse

import pytest
from utils.evidence import FileTask

pytestmark = [pytest.mark.workspace_switch, pytest.mark.claude]

SKILLS_SERVER = "databricks-skill-registry"


def _assert_claude_reports_missing(result) -> None:
    assert result.returncode != 0, result.stdout
    output = (result.stdout + result.stderr).lower()
    expected = (
        "no mcp server configured with that name",
        "no mcp server found with name",
        "no mcp server named",
        "mcp server not found",
    )
    assert any(message in output for message in expected), output


def test_ug_configure_claude_cleans_stale_skills_mcp_on_workspace_switch(
    live_session, workspace, second_workspace
):
    """Scenario: configure the managed workspace, register its skills MCP connection, then
    configure a second real workspace in the same fresh home and use Claude there.

    Expected: the switch warns once, removes the old skills server from Claude's real config and
    the new workspace state, preserves the old workspace bucket, remains stable on repeat
    configure, and Claude completes a real task against the second workspace. This covers the
    normal black-box cleanup path; injected subprocess failures stay in unit/component coverage.
    """
    session = live_session
    task = FileTask(session)

    session.run(
        "configure",
        "--agents",
        "claude",
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )
    session.run("skills", timeout=240)

    primary_state = session.workspace_state()
    primary_entry = next(
        server for server in primary_state["mcp_servers"] if server.get("name") == SKILLS_SERVER
    )
    assert urlparse(primary_entry["url"]).hostname == urlparse(workspace).hostname
    assert "claude" in primary_entry["clients"]
    configured = session.run("mcp", "get", SKILLS_SERVER, binary="claude", timeout=60)
    assert urlparse(workspace).hostname in (configured.stdout + configured.stderr)

    second_url = second_workspace
    session.env["DATABRICKS_BEARER"] = os.environ["DATABRICKS_SECOND_BEARER"]
    switched = session.run(
        "configure",
        "--agents",
        "claude",
        "--workspace",
        second_url,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )
    assert switched.stdout.count("Dropping 1 stale MCP entry") == 1, switched.stdout
    assert SKILLS_SERVER in switched.stdout, switched.stdout
    missing = session.run("mcp", "get", SKILLS_SERVER, binary="claude", timeout=60, ok=False)
    _assert_claude_reports_missing(missing)

    state = session.state()
    assert state["current_workspace"] == second_url
    assert all(
        server.get("name") != SKILLS_SERVER
        for server in state["workspaces"][second_url].get("mcp_servers", [])
    )
    assert primary_entry in state["workspaces"][workspace]["mcp_servers"]
    assert second_url in session.run("status").stdout

    repeated = session.run(
        "configure",
        "--agents",
        "claude",
        "--workspace",
        second_url,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )
    assert "Dropping 1 stale MCP entry" not in repeated.stdout, repeated.stdout
    still_missing = session.run("mcp", "get", SKILLS_SERVER, binary="claude", timeout=60, ok=False)
    _assert_claude_reports_missing(still_missing)

    result = session.run(
        "claude",
        "--",
        "-p",
        task.prompt,
        "--output-format",
        "json",
        "--allowedTools",
        "Read",
        timeout=180,
    )
    task.assert_headless_answer("claude", result)
