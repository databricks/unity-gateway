"""CUJ: configure Codex through ug, then complete a turn in the real ChatGPT desktop app on macOS."""

import time
from pathlib import Path

import pytest
from utils.evidence import (
    agent_sessions,
    assert_completed_task_model,
    assistant_answers,
    completed_task_models,
    fresh_id,
)
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
        # Click the composer so the keystrokes cannot land in another control.
        app.click("AXTextArea", None, timeout=120)
        app.type_line(f"Reply with exactly {marker} and nothing else.")
        app.wait_for("AXStaticText", marker, 300)
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


def test_macos_missing_click_records_native_accessibility_tree(live_session):
    """Scenario: request a nonexistent control from the real desktop Accessibility helper.

    Expected: the native helper fails with the requested label and flushes its application
    tree to the captured command output before trapping. No control is clicked.
    """
    session = live_session
    label = f"missing-{fresh_id()}"
    with MacApp(session, CHATGPT_APP, "chatgpt") as app:
        app.wait_for("AXApplication", None, timeout=180)
        result = session.run(
            "click",
            str(app.proc.pid),
            "AXRadioButton",
            label,
            "0",
            binary=app.client,
            ok=False,
            timeout=60,
        )

    assert result.returncode != 0
    assert f"No AXRadioButton labelled {label}" in result.stderr
    assert any(line.split("\t")[0] == "AXApplication" for line in result.stdout.splitlines())
