"""CUJs: configure Codex through ug, then use its real interactive session."""

import tomllib
from pathlib import Path

import pytest
from utils.constants import CODEX_TEST_MODEL
from utils.evidence import FileTask
from utils.terminal import AgentTerminal, ConfigureTerminal

pytestmark = [pytest.mark.live, pytest.mark.tui, pytest.mark.codex]


@pytest.mark.smoke
def test_ug_configure_codex_databricks(live_session, workspace):
    """Scenario: configure Codex with Databricks Hosted and use its TUI.

    Expected: configure succeeds, Codex returns a file value through the real
    gateway, exits normally, and can reopen the configuration ug created.
    The generated auth helper uses ug and prints only the supplied bearer.
    Optional AI Tools are disabled; the selected agent version is kept pinned.
    """
    session = live_session
    task = FileTask(session)

    session.run(
        "configure",
        "--agents",
        "codex",
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )
    assert not session.workspace_state().get("provider_services", {}).get("codex")
    # Astra is Codex's current default, but it is heavily rate-limited. Pin a
    # different model so this test validates ug rather than Astra capacity.
    command = [str(session.binary), "codex", "--", "--model", CODEX_TEST_MODEL]

    config = tomllib.loads((session.home / ".codex/ucode.config.toml").read_text())
    helper = config["model_providers"][config["model_provider"]]["auth"]
    assert Path(helper["command"]) == session.binary.with_name("ug")
    assert helper["args"][0] == "auth-token"
    token_result = session.run(
        *helper["args"], binary=helper["command"], strip_ansi=False, timeout=30
    )
    assert token_result.stdout == "<redacted>\n"
    assert token_result.stderr == ""

    with AgentTerminal(session, "codex", command, "first-session") as tui:
        tui.boot()
        tui.submit(task.prompt)
        tui.wait_for_task(task)
        tui.exit_normally()
    task.assert_completed(session, "codex")
    session.assert_not_routed()

    with AgentTerminal(session, "codex", command, "reopen") as tui:
        tui.boot()
        tui.check_input_and_exit()


def test_ug_configure_codex_openai_mps(
    live_session, workspace, codex_provider, codex_provider_model
):
    """Scenario: choose the real OpenAI MPS in ug configure's provider picker.

    Expected: ug saves that provider, and launching Codex without --provider
    uses the saved choice with one of its allowed models to complete a
    file-reading task and exit normally.
    """
    session = live_session
    task = FileTask(session)

    command = [
        str(session.binary),
        "configure",
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    ]
    with ConfigureTerminal(session, "codex", command, "configure-provider") as configure:
        configure.select_agent("Codex")
        configure.choose("How should Codex get its models?", "External Models")
        configure.choose("Select a model provider service:", codex_provider)
        configure.finish(timeout=240)
    assert session.workspace_state()["provider_services"]["codex"] == codex_provider
    assert codex_provider in session.run("status").stdout

    command = [str(session.binary), "codex", "--", "--model", codex_provider_model]
    with AgentTerminal(session, "codex", command, "provider-session") as tui:
        tui.boot()
        tui.submit(task.prompt)
        tui.wait_for_task(task, timeout=300)
        tui.exit_normally()
    task.assert_completed(session, "codex")
    session.assert_not_routed()


def _run_codex_openai_dialect_mps_cuj(session, workspace, provider, model, label):
    """Configure Codex through an OpenAI-dialect MPS, then complete a task on one of its models.

    Azure OpenAI and Microsoft Foundry both speak the OpenAI dialect the gateway fronts for Codex,
    so the CUJ is identical apart from which MPS the picker selects. Codex always sends
    `reasoning.effort`, so a completed task also proves two things a plain routing check would miss:
    the provider-type allowlist accepts the MPS's type for Codex, and the backing deployment is
    reasoning-capable (a non-reasoning model 400s the request's `reasoning.effort`).
    """
    task = FileTask(session)

    command = [
        str(session.binary),
        "configure",
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    ]
    with ConfigureTerminal(session, "codex", command, f"configure-{label}-provider") as configure:
        configure.select_agent("Codex")
        configure.choose("How should Codex get its models?", "External Models")
        configure.choose("Select a model provider service:", provider)
        configure.finish(timeout=240)
    assert session.workspace_state()["provider_services"]["codex"] == provider
    assert provider in session.run("status").stdout

    command = [str(session.binary), "codex", "--", "--model", model]
    with AgentTerminal(session, "codex", command, f"{label}-provider-session") as tui:
        tui.boot()
        tui.submit(task.prompt)
        tui.wait_for_task(task, timeout=300)
        tui.exit_normally()
    task.assert_completed(session, "codex")
    session.assert_not_routed()


def test_ug_configure_codex_azure_openai_mps(
    live_session, workspace, codex_azure_provider, codex_azure_provider_model
):
    """Route Codex through an Azure OpenAI (`azure_openai`) MPS backed by a reasoning model."""
    _run_codex_openai_dialect_mps_cuj(
        live_session, workspace, codex_azure_provider, codex_azure_provider_model, "azure"
    )


def test_ug_configure_codex_foundry_mps(
    live_session, workspace, codex_foundry_provider, codex_foundry_provider_model
):
    """Route Codex through a Microsoft Foundry (`microsoft_foundry`) MPS backed by a reasoning model.

    Foundry speaks the same OpenAI dialect as Azure OpenAI, so this reuses the Azure CUJ; the
    separate test exercises the `microsoft_foundry` provider-type allowlist entry for Codex.
    """
    _run_codex_openai_dialect_mps_cuj(
        live_session, workspace, codex_foundry_provider, codex_foundry_provider_model, "foundry"
    )
