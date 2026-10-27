"""CUJs for running GitHub Copilot CLI on a Unity Catalog model location via ug."""

import pytest
from utils.constants import COPILOT_HEADLESS_FLAGS
from utils.evidence import (
    FileTask,
    assert_completed_task_model,
    assert_no_session_registry_left,
    copilot_selected_models,
)

pytestmark = [pytest.mark.live, pytest.mark.copilot]


def test_ug_copilot_runs_a_task_on_a_model_location(
    live_session, workspace, parent_schema, claude_parent_model
):
    """Scenario: from fresh state, run a headless task with `--model-location` set to the
    model-discovery schema and `--model` naming its Claude-compatible model service.

    Expected: ug sets up the workspace, Copilot starts on that service through `dbx-anthropic`,
    the completed answer contains the fixture value and was sent as the service's full name, and
    no session registry is left.
    """
    session = live_session
    task = FileTask(session)
    session.record("copilot-model-location.json", {"location": parent_schema})

    result = session.run(
        "copilot",
        "--workspace",
        workspace,
        "--model-location",
        parent_schema,
        "--model",
        claude_parent_model,
        "-p",
        task.prompt,
        *COPILOT_HEADLESS_FLAGS,
        timeout=180,
    )

    task.assert_headless_answer("copilot", result)
    assert_completed_task_model(session, "copilot", task.value, claude_parent_model)
    assert f"dbx-anthropic/{claude_parent_model}" in copilot_selected_models(session)
    assert_no_session_registry_left(session)
