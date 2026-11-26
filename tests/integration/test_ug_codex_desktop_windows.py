"""CUJ: configure Codex through ug, then complete a turn in the real ChatGPT desktop app on Windows."""

import time

import pytest
from utils.codex_desktop import (
    MCP_SERVICE,
    SKILL_NAME,
    assert_desktop_skill_mcp_session,
    configure_databricks_token_profile,
    finish_windows_readiness_check,
    wait_for_skill_answer,
    wait_for_submitted_prompt,
    wait_for_windows_sandbox,
    wait_for_windows_setup,
    write_search_skill,
)
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
# How the app's picker names that model, and the composer's model-and-effort picker button.
MANAGED_CODEX_MODEL_LABEL = "5.6 Sol"
MODEL_PICKER = f"{MANAGED_CODEX_MODEL_LABEL} Light"
ANSWER_TIMEOUT = 240


def test_ug_codex_desktop_windows_completes_a_gateway_turn(live_session, workspace):
    """Scenario: configure managed Codex, open the ChatGPT desktop app, and ask Codex for a value.

    Expected: the model picker lists only the managed model, under a readable name. The app shows
    the answer, and its own Codex Desktop rollout, using ug's Databricks provider, records exactly
    that answer in a completed turn whose model is the managed system.ai GPT default. Onboarding
    is skipped through its visible controls with UI Automation and the prompt is typed with real
    keystrokes followed by Send; nothing is seeded. Windows sandbox setup completes through
    its visible controls before Send, or the setup wait accepts it already saved as elevated.
    A failed readiness check is finished through its visible
    Try again at the setup screen and after setup's app-server restart; prompts are never
    resubmitted. Each screen gets 60 seconds, sandbox setup gets
    180 seconds, and the answer four minutes. GUI rendering of other screens, MCP, tracing
    and revert while the app runs are not covered.
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
        # After a readiness recheck the app can run setup without its banner; count from here.
        checkpoint = app.setup_event_checkpoint()
        app.click("ControlType.Button", "Go to ChatGPT")
        app.mark_setup_phase("banner-wait-start")
        if wait_for_windows_setup(app):
            app.mark_setup_phase("setup-click-start")
            app.click("ControlType.Button", "Continue")
        deadline = time.monotonic() + 180
        wait_for_windows_sandbox(app, timeout=max(0, deadline - time.monotonic()))
        app.mark_setup_phase("sandbox-mode-observed")
        app.wait_for_setup_restart(checkpoint, deadline)
        app.mark_setup_phase("first-interaction-start")
        # The picker opens on Enter (an empty line), not on UI Automation's Invoke.
        app.click("ControlType.Button", MODEL_PICKER, trace_picker=True)
        app.type_line("")
        app.click("ControlType.MenuItem", "Select model")
        tree = app.wait_for("ControlType.RadioButton", MANAGED_CODEX_MODEL_LABEL)
        models = [row[1] for row in tree if row[0] == "ControlType.RadioButton"]
        assert models == [MANAGED_CODEX_MODEL_LABEL], models
        app.click("ControlType.Button", MODEL_PICKER)
        # Focus the composer so the keystrokes cannot land in another control.
        app.click("ControlType.Edit", None)
        prompt = f"Reply with exactly {marker} and nothing else."
        app.type_line(prompt, submit=False)
        app.wait_for("ControlType.Edit", prompt)
        finish_windows_readiness_check(app)
        app.click("ControlType.Button", "Send")
        try:
            wait_for_submitted_prompt(session, prompt)
            app.wait_for("ControlType.Text", marker, ANSWER_TIMEOUT)
        finally:
            session.record("agent-sessions.json", agent_sessions(session, "codex"))
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


def test_ug_codex_desktop_windows_skill_calls_mcp(live_session, workspace):
    """Scenario: use an authenticated profile, add web_search, and invoke a skill in the desktop GUI.

    Expected: the existing CI credential is saved through the CLI and its identity verified before
    configure/add. The app and MCP use that token profile with no inherited bearer override.
    Browser OAuth and refresh are not covered. The skill reference supplies the hidden query and token.
    The app displays that token and its Databricks-backed completed turn records successful
    native MCP web_search receipts with the exact query on the managed model. User onboarding,
    Windows sandbox setup and Send use visible controls; the rollout must record the prompt
    before the answer wait. Setup must save elevated sandbox mode; readiness errors get their
    visible recheck at the setup screen and after the app-server restart, before Send. The
    expected search is
    approved with Allow once; no approval/config
    state is seeded. UC skill downloads, connector
    browser sign-in and built-in web search are outside this journey.
    """
    session = live_session
    profile = configure_databricks_token_profile(session, workspace)
    marker = fresh_id()
    query = f"Databricks Unity Catalog documentation {fresh_id()}"
    write_search_skill(session.home, query, marker)
    use_managed_config_fixture(session, "managed_workspace_default")
    session.run(
        "configure",
        "--agents",
        "codex",
        "--profile",
        profile,
        "--use-pat",
        "--skip-upgrade",
        timeout=240,
    )
    session.run("mcp", "add", "--agents", "codex", "--names", MCP_SERVICE, timeout=240)

    with WindowsApp(session, CHATGPT_PACKAGE, CHATGPT_EXECUTABLE, "chatgpt") as app:
        app.click("ControlType.RadioButton", "Engineering")
        app.click("ControlType.Button", "Continue")
        app.click("ControlType.Button", "Skip")
        # After a readiness recheck the app can run setup without its banner; count from here.
        checkpoint = app.setup_event_checkpoint()
        app.click("ControlType.Button", "Go to ChatGPT")
        try:
            app.mark_setup_phase("banner-wait-start")
            if wait_for_windows_setup(app):
                app.mark_setup_phase("setup-click-start")
                app.click("ControlType.Button", "Continue")
            deadline = time.monotonic() + 180
            wait_for_windows_sandbox(app, timeout=max(0, deadline - time.monotonic()))
            app.mark_setup_phase("sandbox-mode-observed")
            app.wait_for_setup_restart(checkpoint, deadline)
            app.mark_setup_phase("first-interaction-start")
            app.click("ControlType.Edit", None)
            prompt = f"Use ${SKILL_NAME}. Follow its instructions and reply with only its completion token."
            app.type_line(prompt, submit=False)
            app.wait_for("ControlType.Edit", prompt)
            finish_windows_readiness_check(app)
            app.click("ControlType.Button", "Send")
            wait_for_submitted_prompt(session, prompt)
            wait_for_skill_answer(
                app, query, marker, "ControlType.Text", "ControlType.Button", ANSWER_TIMEOUT
            )
        finally:
            session.record("agent-sessions.json", agent_sessions(session, "codex"))
        deadline = time.monotonic() + 60
        while not completed_task_models(session, "codex", marker):
            assert time.monotonic() < deadline, "The app never recorded the completed skill turn"
            time.sleep(1)
        assert_desktop_skill_mcp_session(session, query, marker, MANAGED_CODEX_MODEL)
