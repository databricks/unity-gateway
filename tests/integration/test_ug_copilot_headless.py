"""CUJs for using GitHub Copilot CLI from scripts through installed ug."""

import pytest
from utils.constants import CLAUDE_TEST_MODEL, CODEX_TEST_MODEL, COPILOT_HEADLESS_FLAGS
from utils.evidence import (
    FileTask,
    assert_completed_task_model,
    assert_no_bearer_on_disk,
    assert_no_session_registry_left,
    copilot_selected_models,
)

pytestmark = [pytest.mark.live, pytest.mark.copilot]


def test_ug_copilot_headless_completes_file_task(live_session, workspace):
    """Scenario: configure Copilot, then run a headless Claude prompt through `ug copilot`.

    Expected: Copilot starts on the registry's Anthropic provider, reads the fixture through
    the session's loopback proxy, and its completed answer (sent as the requested model)
    contains the unknown value; no file under the session home holds the Databricks
    credential, and the session registry is gone afterwards.
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
    assert_completed_task_model(session, "copilot", task.value, CLAUDE_TEST_MODEL)
    assert copilot_selected_models(session)[0] == f"dbx-anthropic/{CLAUDE_TEST_MODEL}"
    assert_no_bearer_on_disk(session)
    assert_no_session_registry_left(session)


def test_ug_copilot_headless_gpt_responses_task(live_session, workspace):
    """Scenario: configure Copilot, then run a headless GPT prompt through `ug copilot`.

    Expected: Copilot starts on the registry's Responses provider, reaches the gateway's
    Responses API through the session proxy, and its completed answer (sent as the requested
    model) contains the fixture's unknown value; no file under the session home holds the
    Databricks credential, and the session registry is gone afterwards.
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
    assert_completed_task_model(session, "copilot", task.value, CODEX_TEST_MODEL)
    assert copilot_selected_models(session)[0] == f"dbx-responses/{CODEX_TEST_MODEL}"
    assert_no_bearer_on_disk(session)
    assert_no_session_registry_left(session)
