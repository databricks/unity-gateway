"""CUJs for using codex from scripts through installed ug."""

import json

import pytest
from utils.constants import CODEX_TEST_MODEL
from utils.evidence import FileTask, assert_completed_task_model

pytestmark = [pytest.mark.live, pytest.mark.codex]


@pytest.mark.smoke
def test_ug_codex_headless_prompt_argument(live_session, workspace):
    """Scenario: configure codex and submit a headless prompt via argument.

    Expected: the real agent reads the fixture and returns its unknown value in
    its structured completed answer, with exit code zero and JSONL-only stdout.
    """
    session = live_session
    session.choose_codex_windows_sandbox()
    task = FileTask(session)
    session.run(
        "configure",
        "--agents",
        "codex",
        "--workspace",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )

    result = session.run(
        "codex",
        "--",
        "exec",
        "--skip-git-repo-check",
        "--json",
        "--model",
        CODEX_TEST_MODEL,
        task.prompt,
        timeout=180,
    )
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert events and all(isinstance(event, dict) for event in events)
    task.assert_headless_answer("codex", result)
    session.assert_not_routed()


def test_ug_codex_headless_prompt_stdin(live_session, workspace):
    """Scenario: configure codex and submit a headless prompt via stdin.

    Expected: the real agent reads the fixture and returns its unknown value in
    its structured completed answer, with exit code zero and JSONL-only stdout.
    """
    session = live_session
    session.choose_codex_windows_sandbox()
    task = FileTask(session)
    session.run(
        "configure",
        "--agents",
        "codex",
        "--workspace",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )

    result = session.run(
        "codex",
        "--",
        "exec",
        "--skip-git-repo-check",
        "--json",
        "--model",
        CODEX_TEST_MODEL,
        "-",
        timeout=180,
        input_text=task.prompt + "\n",
    )
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert events and all(isinstance(event, dict) for event in events)
    task.assert_headless_answer("codex", result)
    session.assert_not_routed()


def test_ug_codex_headless_prompt_after_separator(live_session, workspace):
    """Scenario: configure codex and submit a headless prompt via after separator.

    Expected: the real agent reads the fixture and returns its unknown value in
    its structured completed answer, with exit code zero.
    """
    session = live_session
    session.choose_codex_windows_sandbox()
    task = FileTask(session)
    session.run(
        "configure",
        "--agents",
        "codex",
        "--workspace",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )

    result = session.run(
        "codex",
        "--",
        "exec",
        "--skip-git-repo-check",
        "--json",
        "--model",
        CODEX_TEST_MODEL,
        "--",
        task.prompt,
        timeout=180,
    )
    task.assert_headless_answer("codex", result)
    session.assert_not_routed()


@pytest.mark.parametrize("model_form", ["separate", "equals", "short"])
@pytest.mark.parametrize("model_position", ["before_separator", "exec"])
def test_ug_codex_headless_explicit_model_bypasses_routing(
    live_session, workspace, model_form, model_position
):
    """Scenario: choose a model before ug's separator or within Codex exec.

    Expected: the completed file-task turn selects the requested model, and
    no routing wrapper overrides it.
    """
    session = live_session
    session.choose_codex_windows_sandbox()
    task = FileTask(session)
    session.run(
        "configure",
        "--agents",
        "codex",
        "--workspace",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )
    model = session.model_for_explicit_case("codex")
    model_args = ["--model", model] if model_form == "separate" else [f"--model={model}"]
    if model_form == "short":
        model_args = ["-m", model]
    session.env["ENABLE_SMART_ROUTING_V2"] = "1"
    result = session.run(
        "codex",
        *(model_args if model_position == "before_separator" else []),
        "--",
        "exec",
        "--skip-git-repo-check",
        "--json",
        task.prompt,
        *(model_args if model_position == "exec" else []),
        timeout=180,
    )
    task.assert_headless_answer("codex", result)
    assert_completed_task_model(session, "codex", task.value, model)
    session.assert_not_routed()


@pytest.mark.usefixtures("unmanaged_workspace")
def test_ug_codex_headless_fresh_workspace(live_session, workspace):
    """Scenario: launch Codex headlessly against an unmanaged workspace from fresh state.

    Expected: ``ug codex --workspace`` starts the real installed Codex CLI without a
    configure step, and its structured completed answer contains the unpredictable fixture
    value after using the workspace model; the command exits successfully without routing.
    """
    session = live_session
    task = FileTask(session)

    result = session.run(
        "codex",
        "--workspace",
        workspace,
        "--",
        "exec",
        "--skip-git-repo-check",
        "--json",
        "--model",
        CODEX_TEST_MODEL,
        task.prompt,
        timeout=180,
    )
    task.assert_headless_answer("codex", result)
    session.assert_not_routed()


@pytest.mark.usefixtures("unmanaged_workspace")
def test_ug_codex_fresh_provider_launch(
    live_session, workspace, codex_provider, codex_provider_model
):
    """Scenario: launch Codex from fresh state with an explicit provider service.

    Expected: ``ug codex --workspace`` with ``--provider`` starts the real installed Codex
    CLI without a configure step, reports the selected provider, and exposes exactly its model
    through the real app-server; ``--version`` exits without inference or routing.
    """
    session = live_session
    result = session.run(
        "codex",
        "--workspace",
        workspace,
        "--provider",
        codex_provider,
        "--",
        "--version",
        timeout=180,
    )
    assert codex_provider in result.stdout
    models = session.codex_model_ids(
        [
            "--workspace",
            workspace,
            "--provider",
            codex_provider,
            "--",
            "app-server",
            "--listen",
            "stdio://",
        ]
    )
    assert models == [codex_provider_model]
    session.assert_not_routed()
