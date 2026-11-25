"""CUJ: configure Codex through ug, then complete a turn in the real ChatGPT desktop app on macOS."""

import os
import time
from pathlib import Path

import pytest
from utils.codex_desktop import (
    MCP_SERVICE,
    SKILL_NAME,
    assert_desktop_skill_mcp_session,
    configure_databricks_token_profile,
    wait_for_skill_answer,
    wait_for_submitted_prompt,
    write_search_skill,
)
from utils.evidence import (
    agent_sessions,
    assert_completed_task_model,
    assistant_answers,
    completed_task_models,
    fresh_id,
)
from utils.macos_app import OSASCRIPT, MacApp
from utils.managed import use_managed_config_fixture

pytestmark = [pytest.mark.managed_fixture, pytest.mark.macos_desktop, pytest.mark.codex]

# The CI job installs this app at a pinned version and the harness launches it with its updater
# off, so every launch must run exactly that build. Harness-latest installs the newest appcast
# build instead and leaves this unset.
CHATGPT_APP = Path("/Applications/ChatGPT.app")
PINNED_APP_VERSION = os.environ.get("CHATGPT_APP_VERSION") or None
# The managed_workspace_default fixture's Codex default model.
MANAGED_CODEX_MODEL = "system.ai.gpt-5-6-sol"


def dismiss_skip_setup_dialog(app: MacApp) -> None:
    """Dismiss the Skip setup? dialog and verify the app left that screen.

    One native command activates the app, waits for the button and clicks it, so the wait and
    the click can no longer race on two traversals. The app can drop that click, so it is
    repeated while the dialog stays. A dialog that survives fails with the stuck AX tree
    recorded."""
    app.click_until_gone("AXButton", "Go to ChatGPT", 120)


def type_prompt(app: MacApp, prompt: str) -> None:
    deadline = time.monotonic() + 180
    while True:
        assert time.monotonic() < deadline, "ChatGPT never accepted the prompt"
        app.click("AXTextArea", None, timeout=120)
        app.session.run(
            "-e",
            'on run argv\ntell application "System Events"\n'
            "set frontmost of (first process whose unix id is (item 1 of argv as integer)) to true\n"
            'keystroke "a" using command down\nkey code 51\nend tell\nend run',
            str(app.proc.pid),
            binary=OSASCRIPT,
            timeout=60,
        )
        app.type_line(prompt, submit=False)
        try:
            app.wait_for("AXTextArea", prompt, 10)
            return
        except AssertionError as error:
            if str(error) != f"{app.name} never showed AXTextArea {prompt!r}":
                raise


def test_ug_codex_desktop_macos_completes_a_gateway_turn(live_session, workspace):
    """Scenario: configure managed Codex, open the ChatGPT desktop app, and ask Codex for a value.

    Expected: the app shows the answer, and its own Codex Desktop rollout, using ug's Databricks
    provider, records exactly that answer in a completed turn whose model is the managed system.ai
    GPT default. Onboarding is skipped through its visible buttons with one native click at each
    button's current frame, repeated on the Skip setup? confirmation while it stays, and the prompt
    is typed with real keystrokes, then submitted with Send; nothing is seeded. GUI rendering of other screens, MCP, tracing
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
        assert not PINNED_APP_VERSION or app.version == PINNED_APP_VERSION, app.version
        app.click("AXRadioButton", "Engineering", timeout=180)
        app.click("AXButton", "Continue")
        app.click("AXButton", "Skip")
        dismiss_skip_setup_dialog(app)
        prompt = f"Reply with exactly {marker} and nothing else."
        type_prompt(app, prompt)
        app.click("AXButton", "Send")
        try:
            wait_for_submitted_prompt(session, prompt)
            app.wait_for("AXStaticText", marker, 300)
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

        missing_click = session.run(
            "click",
            str(app.proc.pid),
            "AXButton",
            "__ug_missing_target__",
            "1",
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
        # Traversal failures surface between the tree and the fatal error, prefixed to stay
        # separable from the four-column tree rows.
        tree_rows = [
            line for line in lines[1:fatal_index] if line and not line.startswith("AX error: ")
        ]
        assert tree_rows, lines
        assert all(len(row.split("\t")) == 4 for row in tree_rows), tree_rows
        assert any(row.split("\t")[0].startswith("AX") for row in tree_rows), tree_rows


def test_ug_codex_desktop_macos_skill_calls_mcp(live_session, workspace):
    """Scenario: use an authenticated profile, add web_search, and invoke a skill in the desktop GUI.

    Expected: the existing CI credential is saved through the CLI and its identity verified before
    configure/add. The app and MCP use that token profile with no inherited bearer override.
    Browser OAuth and refresh are not covered. The skill reference supplies the hidden query and token.
    The app displays that token and its Databricks-backed completed turn records successful
    native MCP web_search receipts with the exact query on the managed model. User onboarding
    and Send use visible controls; the rollout must record the submitted prompt before the
    answer wait. The expected search is approved with Allow once; no approval/config state is
    seeded. UC skill downloads, connector
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

    with MacApp(session, CHATGPT_APP, "chatgpt") as app:
        assert not PINNED_APP_VERSION or app.version == PINNED_APP_VERSION, app.version
        app.click("AXRadioButton", "Engineering", timeout=180)
        app.click("AXButton", "Continue")
        app.click("AXButton", "Skip")
        dismiss_skip_setup_dialog(app)
        try:
            prompt = f"Use ${SKILL_NAME}. Follow its instructions and reply with only its completion token."
            type_prompt(app, prompt)
            app.click("AXButton", "Send")
            wait_for_submitted_prompt(session, prompt)
            wait_for_skill_answer(app, query, marker, "AXStaticText", "AXButton", 300)
        finally:
            session.record("agent-sessions.json", agent_sessions(session, "codex"))
        deadline = time.monotonic() + 60
        while not completed_task_models(session, "codex", marker):
            assert time.monotonic() < deadline, "The app never recorded the completed skill turn"
            time.sleep(1)
        assert_desktop_skill_mcp_session(session, query, marker, MANAGED_CODEX_MODEL)
