"""CUJs for inspecting claude commands through ug."""

import pytest

pytestmark = [pytest.mark.live, pytest.mark.claude]


def test_ug_claude_auth_help(live_session, workspace):
    """Scenario: configure claude, then ask ug for auth help.

    Expected: the real claude help is returned, with no routing wrapper or
    model request. This verifies command dispatch, not an interactive session.
    """
    session = live_session
    session.run(
        "configure",
        "--agents",
        "claude",
        "--workspace",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )
    expected = session.run("auth", "--help", binary="claude").stdout.strip()
    actual = session.run("claude", "--", "auth", "--help").stdout
    assert expected and expected in actual, actual
    session.assert_not_routed()


def test_ug_claude_mcp_help(live_session, workspace):
    """Scenario: configure claude, then ask ug for mcp help.

    Expected: the real claude help is returned, with no routing wrapper or
    model request. This verifies command dispatch, not an interactive session.
    """
    session = live_session
    session.run(
        "configure",
        "--agents",
        "claude",
        "--workspace",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )
    expected = session.run("mcp", "--help", binary="claude").stdout.strip()
    actual = session.run("claude", "--", "mcp", "--help").stdout
    assert expected and expected in actual, actual
    session.assert_not_routed()
