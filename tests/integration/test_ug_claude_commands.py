"""CUJs for inspecting claude commands through ug."""

import pytest

pytestmark = [pytest.mark.live, pytest.mark.claude]


@pytest.mark.parametrize(
    "SMART_ROUTER_CONFIG_VERSION",
    [
        None,
        "first_prompt_and_subagent_no_orch_v0",
        "subagent_only_v0",
        "subagent_only_v1",
        "subagent_orch_v0",
    ],
    ids=[
        "unconfigured",
        "first_prompt_and_subagent_no_orch_v0",
        "subagent_only_v0",
        "subagent_only_v1",
        "subagent_orch_v0",
    ],
)
def test_ug_claude_auth_help(live_session, workspace, SMART_ROUTER_CONFIG_VERSION):
    """Scenario: configure claude, then ask ug for auth help under each selector.

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
    if SMART_ROUTER_CONFIG_VERSION is not None:
        session.env["SMART_ROUTER_CONFIG_VERSION"] = SMART_ROUTER_CONFIG_VERSION
    expected = session.run("auth", "--help", binary="claude").stdout.strip()
    actual = session.run("claude", "--", "auth", "--help").stdout
    assert expected and expected in actual, actual
    session.assert_not_routed()


@pytest.mark.parametrize(
    "SMART_ROUTER_CONFIG_VERSION",
    [
        None,
        "first_prompt_and_subagent_no_orch_v0",
        "subagent_only_v0",
        "subagent_only_v1",
        "subagent_orch_v0",
    ],
    ids=[
        "unconfigured",
        "first_prompt_and_subagent_no_orch_v0",
        "subagent_only_v0",
        "subagent_only_v1",
        "subagent_orch_v0",
    ],
)
def test_ug_claude_mcp_help(live_session, workspace, SMART_ROUTER_CONFIG_VERSION):
    """Scenario: configure claude, then ask ug for mcp help under each selector.

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
    if SMART_ROUTER_CONFIG_VERSION is not None:
        session.env["SMART_ROUTER_CONFIG_VERSION"] = SMART_ROUTER_CONFIG_VERSION
    expected = session.run("mcp", "--help", binary="claude").stdout.strip()
    actual = session.run("claude", "--", "mcp", "--help").stdout
    assert expected and expected in actual, actual
    session.assert_not_routed()
