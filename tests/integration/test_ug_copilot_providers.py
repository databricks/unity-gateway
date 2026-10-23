"""CUJs for running GitHub Copilot CLI through a Unity Catalog Model Provider Service via ug."""

import pytest
from utils.constants import COPILOT_HEADLESS_FLAGS
from utils.evidence import (
    FileTask,
    assert_completed_task_model,
    assert_no_session_registry_left,
    copilot_selected_models,
)

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


def _run_task(session, provider, *model_args):
    task = FileTask(session)
    result = session.run(
        "copilot",
        "--provider",
        provider,
        *model_args,
        "-p",
        task.prompt,
        *COPILOT_HEADLESS_FLAGS,
        timeout=180,
    )
    return task, result


def test_ug_copilot_runs_a_task_through_an_anthropic_provider(
    live_session, workspace, claude_provider, claude_provider_model
):
    """Scenario: configure Copilot, then run a headless task with `--provider` set to an API-key
    Anthropic provider service and no `--model`.

    Expected: ug names the provider, Copilot starts on the service's Claude model through
    `dbx-anthropic`, the completed answer contains the fixture value and was sent as that model,
    and no session registry is left.
    """
    session = live_session
    _configure(session, workspace)
    session.record("copilot-provider.json", {"provider": claude_provider})

    task, result = _run_task(session, claude_provider)

    assert claude_provider in result.stderr + result.stdout
    task.assert_headless_answer("copilot", result)
    assert_completed_task_model(session, "copilot", task.value, claude_provider_model)
    assert f"dbx-anthropic/{claude_provider_model}" in copilot_selected_models(session)
    assert_no_session_registry_left(session)


def test_ug_copilot_runs_a_task_through_an_openai_provider(
    live_session, workspace, codex_provider, codex_provider_model
):
    """Scenario: configure Copilot, then run a headless task through an OpenAI provider service
    on its allowed GPT model.

    Expected: the session runs on `dbx-responses/<model>`, the completed answer contains the
    fixture value and was sent as that model, and no session registry is left.
    """
    session = live_session
    _configure(session, workspace)
    session.record("copilot-provider.json", {"provider": codex_provider})

    task, result = _run_task(session, codex_provider, "--model", codex_provider_model)

    task.assert_headless_answer("copilot", result)
    assert_completed_task_model(session, "copilot", task.value, codex_provider_model)
    assert f"dbx-responses/{codex_provider_model}" in copilot_selected_models(session)
    assert_no_session_registry_left(session)


def test_ug_copilot_runs_a_task_through_an_azure_openai_provider(
    live_session, workspace, codex_azure_provider, codex_azure_provider_model
):
    """Scenario: configure Copilot, then run a headless task through an Azure OpenAI provider
    service on its GPT model.

    Expected: the session runs on `dbx-responses/<model>` through the gateway's native OpenAI
    route, the completed answer contains the fixture value and was sent as that model, and no
    session registry is left.
    """
    session = live_session
    _configure(session, workspace)
    session.record("copilot-provider.json", {"provider": codex_azure_provider})

    task, result = _run_task(session, codex_azure_provider, "--model", codex_azure_provider_model)

    task.assert_headless_answer("copilot", result)
    assert_completed_task_model(session, "copilot", task.value, codex_azure_provider_model)
    assert f"dbx-responses/{codex_azure_provider_model}" in copilot_selected_models(session)
    assert_no_session_registry_left(session)


def test_ug_copilot_runs_a_task_through_a_microsoft_foundry_provider(
    live_session, workspace, codex_foundry_provider, codex_foundry_provider_model
):
    """Scenario: configure Copilot, then run a headless task through a Microsoft Foundry provider
    service on its GPT model.

    Expected: the session runs on `dbx-responses/<model>` through the gateway's native OpenAI
    route, the completed answer contains the fixture value and was sent as that model, and no
    session registry is left.
    """
    session = live_session
    _configure(session, workspace)
    session.record("copilot-provider.json", {"provider": codex_foundry_provider})

    task, result = _run_task(
        session, codex_foundry_provider, "--model", codex_foundry_provider_model
    )

    task.assert_headless_answer("copilot", result)
    assert_completed_task_model(session, "copilot", task.value, codex_foundry_provider_model)
    assert f"dbx-responses/{codex_foundry_provider_model}" in copilot_selected_models(session)
    assert_no_session_registry_left(session)


def test_ug_copilot_allow_all_bedrock_provider_runs_the_named_model(
    live_session, workspace, claude_bedrock_allow_all_provider, claude_bedrock_allow_all_model
):
    """Scenario: run a headless task through a Bedrock provider service that allows every model
    and declares none, naming its Bedrock Claude id with `--model`.

    Expected: ug accepts the provider, the completed answer contains the fixture value and was
    sent as the Bedrock id, and no session registry is left.
    """
    session = live_session
    _configure(session, workspace)
    session.record("copilot-provider.json", {"provider": claude_bedrock_allow_all_provider})

    task, result = _run_task(
        session, claude_bedrock_allow_all_provider, "--model", claude_bedrock_allow_all_model
    )

    task.assert_headless_answer("copilot", result)
    assert_completed_task_model(session, "copilot", task.value, claude_bedrock_allow_all_model)
    assert_no_session_registry_left(session)


def test_ug_copilot_refuses_a_subscription_relay_provider(
    live_session, workspace, claude_relayed_provider
):
    """Scenario: configure Copilot, then launch it with a provider service that relays a Claude
    subscription.

    Expected: ug exits 1 before starting Copilot, and its error says only Claude Code can use
    that service. No session registry is left.
    """
    session = live_session
    _configure(session, workspace)

    result = session.run(
        "copilot", "--provider", claude_relayed_provider, "-p", "hi", ok=False, timeout=120
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "only Claude Code can use" in result.stderr
    assert not list(session.home.glob(".copilot/session-state/*")), "Copilot started"
    assert_no_session_registry_left(session)
