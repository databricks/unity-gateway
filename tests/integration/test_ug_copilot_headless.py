"""CUJs for using GitHub Copilot CLI from scripts through installed ug."""

import pytest
from utils.constants import CLAUDE_TEST_MODEL, CODEX_TEST_MODEL, COPILOT_HEADLESS_FLAGS
from utils.evidence import FileTask, assert_no_bearer_on_disk

pytestmark = [pytest.mark.live, pytest.mark.copilot]


def test_ug_copilot_headless_completes_file_task(live_session, workspace):
    """Scenario: configure Copilot, then run a headless Claude prompt through `ug copilot`.

    Expected: Copilot reads the fixture through the session's loopback proxy (Anthropic
    provider) and its completed answer contains the unknown value; no file under the
    session home holds the Databricks credential afterwards.
    """
    session = live_session
    task = FileTask(session)
    session.run(
        "configure",
        "--agents",
        "copilot",
        "--workspace",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )
    session.record("copilot-model.json", {"model": CLAUDE_TEST_MODEL})

    result = session.run(
        "copilot",
        "--model",
        CLAUDE_TEST_MODEL,
        "-p",
        task.prompt,
        *COPILOT_HEADLESS_FLAGS,
        timeout=180,
    )

    task.assert_headless_answer("copilot", result)
    assert_no_bearer_on_disk(session)


def test_ug_copilot_headless_gpt_responses_task(live_session, workspace):
    """Scenario: configure Copilot, then run a headless GPT prompt through `ug copilot`.

    Expected: Copilot reaches the gateway's Responses API through the session proxy and
    its completed answer contains the fixture's unknown value; no file under the session
    home holds the Databricks credential afterwards.
    """
    session = live_session
    task = FileTask(session)
    session.run(
        "configure",
        "--agents",
        "copilot",
        "--workspace",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )
    session.record("copilot-model.json", {"model": CODEX_TEST_MODEL})

    result = session.run(
        "copilot",
        "--model",
        CODEX_TEST_MODEL,
        "-p",
        task.prompt_with_path,
        *COPILOT_HEADLESS_FLAGS,
        timeout=180,
    )

    task.assert_headless_answer("copilot", result)
    assert_no_bearer_on_disk(session)
