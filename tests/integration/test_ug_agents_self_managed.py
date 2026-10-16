"""CUJs for self-managing an agent the workspace admin's managed config does not enable.

These run against the managed e2e workspace with the checked-in `managed_workspace_default.json`
CodingAgentConfig injected via UCODE_MANAGED_CONFIG_STUB; it enables Claude and Codex but NOT
OpenCode. The gateway and the agent binaries are real. The invariant under test is OpenCode: hidden
and refused -> self-managed and launchable -> hidden and refused again.
"""

import pytest
from utils.evidence import FileTask
from utils.managed import use_managed_config_fixture

pytestmark = [pytest.mark.managed_fixture, pytest.mark.opencode]

DISABLED_MESSAGE = "doesn't enable OpenCode"
ADD_HINT = "ug agents add opencode"


def _assert_opencode_rejected(session):
    result = session.run("opencode", "--", "run", ok=False, timeout=120)
    assert result.returncode != 0, result.stdout
    assert DISABLED_MESSAGE in result.stderr, result.stderr
    assert ADD_HINT in result.stderr, result.stderr


def test_ug_agents_self_managed_opencode_journey(live_session, workspace):
    """Scenario: on a workspace whose admin config does not enable OpenCode, a developer
    configures, sees OpenCode refused, adds it as self-managed, completes a real headless task,
    then removes it.

    Expected: bare configure applies the admin agents; `ug agents list` omits OpenCode while
    showing an admin-managed agent; launching OpenCode exits nonzero with the "doesn't enable
    OpenCode" guidance; after `ug agents add opencode` the list shows OpenCode as self-managed and
    a real headless Read task returns the fixture value; after `ug agents remove opencode` the list
    omits it and the launch is refused again.
    """
    session = live_session
    use_managed_config_fixture(session, "managed_workspace_default")
    task = FileTask(session)
    result = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    assert "Select coding agents to configure:" not in result.stdout, result.stdout

    listed = session.run("agents", "list")
    assert "OpenCode" not in listed.stdout, listed.stdout
    assert "admin-managed" in listed.stdout, listed.stdout

    _assert_opencode_rejected(session)

    added = session.run("agents", "add", "opencode")
    assert "Added OpenCode as self-managed" in added.stdout, added.stdout

    listed = session.run("agents", "list")
    assert "OpenCode" in listed.stdout and "self-managed" in listed.stdout, listed.stdout

    # No --model: the launch uses the model unmanaged discovery writes; a bare id would be
    # forwarded unchanged and rejected by opencode.
    result = session.run(
        "opencode", "--", "run", "--format", "json", "--auto", task.prompt, timeout=180
    )
    task.assert_headless_answer("opencode", result)

    removed = session.run("agents", "remove", "opencode")
    assert "Removed OpenCode from your self-managed list" in removed.stdout, removed.stdout

    listed = session.run("agents", "list")
    assert "OpenCode" not in listed.stdout, listed.stdout
    _assert_opencode_rejected(session)


def test_ug_agents_admin_managed_guardrails(live_session, workspace):
    """Scenario: a developer tries to add or remove an agent the admin's config already enables.

    Targets the first admin-managed agent shown by `ug agents list`. Only `ug agents` is driven,
    so no agent binary is launched.

    Expected: `agents add` is a no-op noting the workspace admin already manages it; `agents remove`
    is rejected with nonzero exit because the workspace admin manages it.
    """
    session = live_session
    use_managed_config_fixture(session, "managed_workspace_default")
    session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)

    listed = session.run("agents", "list")
    admin_rows = [line for line in listed.stdout.splitlines() if "admin-managed" in line]
    assert admin_rows, listed.stdout
    display = admin_rows[0].split()[0]
    agent = {"Claude": "claude", "Codex": "codex", "OpenCode": "opencode"}.get(display)
    assert agent, admin_rows[0]

    added = session.run("agents", "add", agent)
    assert "already managed by your workspace admin" in added.stdout + added.stderr

    removed = session.run("agents", "remove", agent, ok=False)
    assert removed.returncode != 0, removed.stdout
    assert "managed by your workspace admin and cannot" in removed.stdout + removed.stderr
