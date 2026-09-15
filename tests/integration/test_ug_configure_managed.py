"""CUJs: configure against a managed workspace, where an admin publishes the setup.

These run against the managed e2e workspace (`E2E_ADMIN_WORKSPACE`), which publishes a
CodingAgentConfig. They are the only journeys that exercise the managed path end to end:
`ug configure` applies the admin config to every enabled agent without the personal agent
selector. Later stack layers extend each case with that agent's static model_services.
"""

import pytest


@pytest.mark.managed
@pytest.mark.claude
def test_ug_configure_managed_claude(live_session, workspace):
    """Scenario: run `ug configure` on a workspace that publishes a managed config.

    Expected: ug applies the admin config to every enabled agent without showing the
    personal agent selector; it prints the managed confirmation and exits zero.
    """
    session = live_session
    result = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    assert "Select coding agents to configure:" not in result.stdout, result.stdout
    assert "managed config is published" in result.stdout, result.stdout


@pytest.mark.managed
@pytest.mark.codex
def test_ug_configure_managed_codex(live_session, workspace):
    """Scenario: run `ug configure` on a workspace that publishes a managed config.

    Expected: ug applies the admin config to every enabled agent without showing the
    personal agent selector; it prints the managed confirmation and exits zero.
    """
    session = live_session
    result = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    assert "Select coding agents to configure:" not in result.stdout, result.stdout
    assert "managed config is published" in result.stdout, result.stdout
