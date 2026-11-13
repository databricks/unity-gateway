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
    and revert while the app runs are not covered. A subsequent click on an absent label fails
    with the native accessibility tree before the fatal error, captured in command evidence.
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

        missing_click = session.run(
            "click",
            str(app.proc.pid),
            "AXButton",
            "__ug_missing_target__",
            binary=app.client,
            ok=False,
            timeout=60,
        )
        assert missing_click.returncode != 0
        lines = missing_click.stderr.split("\n")
        assert lines[0] == f"Accessibility tree at failed click (PID {app.proc.pid}):", lines
        fatal_index = next(
            (
                index
                for index, line in enumerate(lines)
                if "Fatal error: No AXButton labelled __ug_missing_target__" in line
            ),
            None,
        )
        assert fatal_index is not None, lines
        tree_rows = [line for line in lines[1:fatal_index] if line]
        assert tree_rows, lines
        assert all(len(row.split("\t")) == 4 for row in tree_rows), tree_rows
        assert any(row.split("\t")[0].startswith("AX") for row in tree_rows), tree_rows
