"""CUJs for using opencode from scripts through installed ug."""

import pytest
from utils.evidence import FileTask

pytestmark = [pytest.mark.live, pytest.mark.opencode]


def test_ug_opencode_headless_prompt_argument(live_session, workspace):
    """Scenario: configure opencode and submit a headless prompt via argument.

    Expected: the real agent completes a Read tool call on the fixture and its
    final text answer contains the unknown value, with exit code zero.
    """
    session = live_session
    task = FileTask(session)
    session.run(
        "configure",
        "--agents",
        "opencode",
        "--workspace",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )

    # No --model: use the default the managed-free configure just discovered and wrote, matching
    # the real `ug configure` → `ug opencode` flow (and avoiding a bare model id ug forwards as-is).
    result = session.run(
        "opencode",
        "--",
        "run",
        "--format",
        "json",
        "--auto",
        task.prompt,
        timeout=180,
    )
    task.assert_headless_answer("opencode", result)
