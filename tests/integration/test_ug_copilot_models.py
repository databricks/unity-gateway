"""CUJs for discovering and switching between Databricks models in GitHub Copilot CLI through ug."""

import pytest
from utils.constants import (
    CLAUDE_TEST_MODEL,
    CODEX_TEST_MODEL,
    COPILOT_HEADLESS_FLAGS,
)
from utils.evidence import (
    FileTask,
    assert_completed_task_model,
    assert_no_session_registry_left,
    copilot_selected_models,
    fresh_id,
)
from utils.terminal import AgentTerminal

pytestmark = [pytest.mark.live, pytest.mark.copilot]


def _configure(session, workspace):
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


def test_ug_copilot_one_setup_runs_claude_then_gpt_by_provider_ref(live_session, workspace):
    """Scenario: configure Copilot once, run a headless Claude task, then a headless GPT task
    selected with Copilot's own `dbx-responses/<model>` form, without reconfiguring.

    Expected: both completed answers contain their fixture values and were sent as the requested
    models, the sessions started on `dbx-anthropic` then `dbx-responses`, and no session registry
    is left afterwards. The Claude model is older than its family's newest, so only the full
    discovered catalog registers it for Copilot.
    """
    session = live_session
    claude_task = FileTask(session)
    gpt_task = FileTask(session)
    _configure(session, workspace)
    session.record("copilot-models.json", {"models": [CLAUDE_TEST_MODEL, CODEX_TEST_MODEL]})

    claude = session.run(
        "copilot",
        "--model",
        CLAUDE_TEST_MODEL,
        "-p",
        claude_task.prompt,
        *COPILOT_HEADLESS_FLAGS,
        timeout=180,
    )
    gpt = session.run(
        "copilot",
        "--model",
        f"dbx-responses/{CODEX_TEST_MODEL}",
        "-p",
        gpt_task.prompt,
        *COPILOT_HEADLESS_FLAGS,
        timeout=180,
    )

    claude_task.assert_headless_answer("copilot", claude)
    gpt_task.assert_headless_answer("copilot", gpt)
    assert_completed_task_model(session, "copilot", claude_task.value, CLAUDE_TEST_MODEL)
    assert_completed_task_model(session, "copilot", gpt_task.value, CODEX_TEST_MODEL)
    selected = copilot_selected_models(session)
    assert f"dbx-anthropic/{CLAUDE_TEST_MODEL}" in selected
    assert f"dbx-responses/{CODEX_TEST_MODEL}" in selected
    assert_no_session_registry_left(session)


def test_ug_copilot_unknown_model_lists_available_models(live_session, workspace):
    """Scenario: configure Copilot, then launch it interactively with a model the workspace
    doesn't have.

    Expected: ug exits 1 before starting Copilot, and its error names the model and lists the
    available ones, including the Claude and GPT test models but not `claude-opus-4-1`, which
    Copilot can't use. No session registry is left.
    """
    session = live_session
    _configure(session, workspace)
    missing = f"system.ai.no-such-model-{fresh_id()}"

    result = session.run("copilot", "--model", missing, ok=False, timeout=120)

    assert result.returncode == 1, result.stdout + result.stderr
    assert f"Model '{missing}' isn't available in ug copilot" in result.stderr
    assert CLAUDE_TEST_MODEL in result.stderr
    assert CODEX_TEST_MODEL in result.stderr
    assert "claude-opus-4-1" not in result.stderr
    assert not list(session.home.glob(".copilot/session-state/*")), "Copilot started"
    assert_no_session_registry_left(session)


def test_ug_copilot_tui_switches_from_claude_to_gpt_with_model_command(live_session, workspace):
    """Scenario: configure Copilot, start its TUI on a Claude model, complete a task, switch to a
    GPT model with `/model dbx-responses/<model>`, and complete a second task in the same session.

    Expected: the folder-trust prompt is answered through its visible choice, both answers carry
    their fixture values and were sent as the Claude then the GPT model, the session records the
    switch to the GPT ref, Copilot exits normally, and no session registry is left.
    """
    session = live_session
    claude_task = FileTask(session)
    gpt_task = FileTask(session)
    _configure(session, workspace)
    session.record("copilot-models.json", {"models": [CLAUDE_TEST_MODEL, CODEX_TEST_MODEL]})
    gpt_ref = f"dbx-responses/{CODEX_TEST_MODEL}"
    command = [
        str(session.binary),
        "copilot",
        "--model",
        CLAUDE_TEST_MODEL,
        "--allow-all-tools",
        "--no-auto-update",
        "--disable-builtin-mcps",
    ]

    with AgentTerminal(session, "copilot", command, "copilot-model-switch") as tui:
        tui.boot()
        tui.submit(claude_task.prompt)
        tui.wait_for_task(claude_task)
        tui.submit(f"/model {gpt_ref}")
        tui.wait_for(lambda screen: "Model changed" in screen, "the model switch notice")
        tui.submit(gpt_task.prompt)
        tui.wait_for_task(gpt_task)
        tui.exit_normally()

    assert_completed_task_model(session, "copilot", claude_task.value, CLAUDE_TEST_MODEL)
    assert_completed_task_model(session, "copilot", gpt_task.value, CODEX_TEST_MODEL)
    assert copilot_selected_models(session)[-1] == gpt_ref
    assert_no_session_registry_left(session)
