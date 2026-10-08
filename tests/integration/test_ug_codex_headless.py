"""CUJs for using codex from scripts through installed ug."""

import json

import pytest
from utils.constants import CODEX_TEST_MODEL
from utils.evidence import FileTask

pytestmark = [pytest.mark.live, pytest.mark.codex]


@pytest.mark.smoke
def test_ug_codex_headless_prompt_argument(live_session, workspace):
    """Scenario: configure codex and submit a headless prompt via argument.

    Expected: the real agent reads the fixture and returns its unknown value in
    its structured completed answer, with exit code zero and JSONL-only stdout.
    """
    session = live_session
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
@pytest.mark.parametrize(
    "SMART_ROUTER_CONFIG_VERSION",
    [
        "first_prompt_and_subagent_no_orch_v0",
        "subagent_only_v0",
        "subagent_only_v1",
        "subagent_orch_v0",
    ],
    ids=[
        "first_prompt_and_subagent_no_orch_v0",
        "subagent_only_v0",
        "subagent_only_v1",
        "subagent_orch_v0",
    ],
)
def test_ug_codex_headless_explicit_model_bypasses_routing(
    live_session, workspace, model_form, SMART_ROUTER_CONFIG_VERSION
):
    """Scenario: choose an explicit model under each supported routing selector.

    Expected: the model option is accepted, the real file task completes, and
    no routing wrapper overrides the caller's choice.
    """
    session = live_session
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
    session.env["SMART_ROUTER_CONFIG_VERSION"] = SMART_ROUTER_CONFIG_VERSION
    result = session.run(
        "codex",
        "--",
        "exec",
        "--skip-git-repo-check",
        "--json",
        task.prompt,
        *model_args,
        timeout=180,
    )
    task.assert_headless_answer("codex", result)
    session.assert_not_routed()
