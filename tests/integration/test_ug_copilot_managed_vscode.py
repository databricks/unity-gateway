"""CUJ: a workspace's managed config turns off GitHub Copilot in VS Code."""

import pytest
from utils.managed import use_managed_config_fixture

pytestmark = [pytest.mark.live, pytest.mark.copilot, pytest.mark.managed_fixture]


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_ug_configure_honors_managed_copilot_vscode_off(live_session, workspace):
    """Scenario: run `ug configure` under a managed config whose GitHub Copilot entry sets
    `vscode_integration.enabled` to false, then `ug status`.

    Expected: configure succeeds and says the workspace turns off GitHub Copilot in VS Code,
    rather than looking for VS Code's `code` command, and `ug status` shows VS Code as off.
    """
    session = live_session
    use_managed_config_fixture(session, "copilot_vscode_off")

    result = session.run(
        "configure",
        "--workspace",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )

    output = _flat(result.stdout)
    assert "turns off GitHub Copilot in VS Code" in output, result.stdout
    assert "`code` command wasn't found" not in output, result.stdout
    status = session.run("status")
    assert "VS Code: off (workspace-managed)" in _flat(status.stdout), status.stdout
