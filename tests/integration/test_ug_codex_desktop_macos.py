"""CUJ: configure Codex through ug, then complete a turn in the real ChatGPT desktop app on macOS."""

from pathlib import Path

import pytest
from utils.evidence import agent_sessions, assert_completed_task_model, assistant_answers, fresh_id
from utils.macos_app import MacApp
from utils.managed import use_managed_config_fixture

pytestmark = [pytest.mark.managed_fixture, pytest.mark.macos_desktop, pytest.mark.codex]

# The CI job installs this app at a pinned version.
CHATGPT_APP = Path("/Applications/ChatGPT.app")
# The managed_workspace_default fixture's Codex default model.
MANAGED_CODEX_MODEL = "system.ai.gpt-5-6-sol"


def test_ug_codex_desktop_macos_completes_a_gateway_turn(live_session, workspace):
    """Scenario: configure managed Codex, open the ChatGPT desktop app, and ask Codex for a value.

    Expected: the app shows the answer, and its own Codex Desktop rollout, using ug's Databricks
    provider, records exactly that answer in a completed turn whose model is the managed system.ai
    GPT default. Onboarding is skipped through its visible buttons with mouse clicks and the prompt
    is typed with real keystrokes; nothing is seeded. GUI rendering of other screens, MCP, tracing
    and revert while the app runs are not covered.
    """
    session = live_session
    marker = fresh_id()
    use_managed_config_fixture(session, "managed_workspace_default")
    session.run(
        "configure", "--agents", "codex", "--workspace", workspace, "--skip-upgrade", timeout=240
    )

    with MacApp(session, CHATGPT_APP, "chatgpt") as app:
        app.click("AXRadioButton", "Engineering", timeout=180)
        app.click("AXButton", "Continue")
        app.click("AXButton", "Skip")
        app.click("AXButton", "Go to ChatGPT")
        app.wait_for("AXTextArea", None, 120)
        app.type_line(f"Reply with exactly {marker} and nothing else.")
        app.wait_for("AXStaticText", marker, 300)

    sessions = agent_sessions(session, "codex").values()
    origins = [
        (record["payload"]["originator"], record["payload"]["model_provider"])
        for records in sessions
        for record in records
        if record.get("type") == "session_meta"
    ]
    assert origins == [("Codex Desktop", "Databricks")], origins
    answers = [answer for records in sessions for answer in assistant_answers("codex", records)]
    assert [answer.strip() for answer in answers] == [marker], answers
    assert_completed_task_model(session, "codex", marker, MANAGED_CODEX_MODEL)
