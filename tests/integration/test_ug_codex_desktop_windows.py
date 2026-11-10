"""CUJ: configure Codex through ug, then complete a turn in the real ChatGPT desktop app on Windows."""

import time

import pytest
from utils.evidence import (
    agent_sessions,
    assert_completed_task_model,
    assistant_answers,
    completed_task_models,
    fresh_id,
)
from utils.managed import use_managed_config_fixture
from utils.windows_app import WindowsApp

pytestmark = [pytest.mark.managed_fixture, pytest.mark.windows_desktop, pytest.mark.codex]

# The CI job installs this MSIX package at a pinned version.
CHATGPT_PACKAGE = "OpenAI.Codex"
CHATGPT_EXECUTABLE = "app/ChatGPT.exe"
# The managed_workspace_default fixture's Codex default model.
MANAGED_CODEX_MODEL = "system.ai.gpt-5-6-sol"
ANSWER_TIMEOUT = 240


def test_ug_codex_desktop_windows_completes_a_gateway_turn(live_session, workspace):
    """Scenario: configure managed Codex, open the ChatGPT desktop app, and ask Codex for a value.

    Expected: the app shows the answer, and its own Codex Desktop rollout, using ug's Databricks
    provider, records exactly that answer in a completed turn whose model is the managed system.ai
    GPT default. Onboarding is skipped through its visible controls with UI Automation and the
    prompt is typed with real keystrokes; nothing is seeded. Each screen gets 60 seconds and the
    answer four minutes, without retries. GUI rendering of other screens, MCP, tracing and revert
    while the app runs are not covered.
    """
    session = live_session
    marker = fresh_id()
    use_managed_config_fixture(session, "managed_workspace_default")
    session.run(
        "configure", "--agents", "codex", "--workspace", workspace, "--skip-upgrade", timeout=240
    )

    with WindowsApp(session, CHATGPT_PACKAGE, CHATGPT_EXECUTABLE, "chatgpt") as app:
        app.click("ControlType.RadioButton", "Engineering")
        app.click("ControlType.Button", "Continue")
        app.click("ControlType.Button", "Skip")
        app.click("ControlType.Button", "Go to ChatGPT")
        # Focus the composer so the keystrokes cannot land in another control.
        app.click("ControlType.Edit", None)
        app.type_line(f"Reply with exactly {marker} and nothing else.")
        app.wait_for("ControlType.Text", marker, ANSWER_TIMEOUT)
        # The answer can render before the rollout records the completed turn; keep the app open.
        deadline = time.monotonic() + 60
        while not completed_task_models(session, "codex", marker):
            assert time.monotonic() < deadline, "The app never recorded the completed turn"
            time.sleep(1)

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
