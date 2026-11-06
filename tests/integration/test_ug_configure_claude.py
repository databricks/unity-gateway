"""CUJs: configure Claude through ug, then use its real interactive session."""

import json
from pathlib import Path

import pytest
from utils.desktop import assert_desktop_model_labels
from utils.evidence import FileTask, assert_completed_task_model
from utils.harness import split_command
from utils.terminal import AgentTerminal, ConfigureTerminal

pytestmark = [pytest.mark.live, pytest.mark.tui, pytest.mark.claude]

# Bedrock reports the bare Anthropic id, not the us.anthropic.* slug the MPS targets.
BEDROCK_SONNET_RESPONSE_MODEL = "claude-sonnet-5-5"


@pytest.mark.smoke
def test_ug_configure_claude_databricks(live_session, workspace, desktop_cli_profile):
    """Scenario: configure Databricks Hosted, launch plain ug claude, and open /model.

    Expected: without managed config, a provider, a model location, or a discovery
    flag, Claude caches system.ai models from the gateway and shows a discovered
    model in its real picker. It returns a file value through the real gateway,
    exits normally, and can reopen the configuration ug created.
    The generated auth helper uses ug and prints only the supplied bearer.
    Optional AI Tools are disabled; the selected agent version is kept pinned.
    The Desktop profile preserves the hosted Haiku routing ID and labels it Claude Haiku 4.5.
    """
    session = live_session
    task = FileTask(session)

    # Configure using the installed public CLI (no post-configure probe runs).
    session.run(
        "configure",
        "--agents",
        "claude",
        "--profile",
        desktop_cli_profile,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )
    assert not session.workspace_state().get("provider_services", {}).get("claude")
    assert_desktop_model_labels(session, {"system.ai.claude-haiku-4-5": "Claude Haiku 4.5"})

    settings = json.loads((session.home / ".claude/ucode-settings.json").read_text())
    helper = split_command(settings["apiKeyHelper"])
    assert Path(helper[0]).with_suffix("") == session.binary.with_name("ug")
    assert helper[1] == "auth-token"
    token_result = session.run(*helper[1:], binary=helper[0], strip_ansi=False, timeout=30)
    assert token_result.stdout == "<redacted>\n"
    assert token_result.stderr == ""

    # Use the real TUI; a config file or startup banner alone is not success.
    with AgentTerminal(session, "claude", [str(session.binary), "claude"], "first-session") as tui:
        tui.boot()
        tui.submit(task.prompt)
        tui.wait_for_task(task)
        screen = tui.open_model_picker(
            model_visible=lambda _text: session.claude_gateway_cache_ready()
        )
        cache = json.loads((session.home / ".claude/cache/gateway-models.json").read_text())
        assert cache["baseUrl"] == workspace.rstrip("/") + "/ai-gateway/anthropic", cache
        models = cache["models"]
        system_ai_models = [model for model in models if model["id"].startswith("system.ai.")]
        assert system_ai_models, cache
        assert any(
            model["id"] in screen or (model.get("display_name") and model["display_name"] in screen)
            for model in system_ai_models
        ), screen
        tui.exit_normally()
    task.assert_completed(session, "claude")
    session.assert_not_routed()

    # Reopen the same home, without configuring again or seeding onboarding state.
    with AgentTerminal(session, "claude", [str(session.binary), "claude"], "reopen") as tui:
        tui.boot()
        tui.check_input_and_exit()


def test_ug_configure_claude_anthropic_mps(
    live_session, claude_provider, claude_provider_model, desktop_cli_profile
):
    """Scenario: choose the real Anthropic MPS in ug configure's provider picker.

    Expected: ug saves that provider, and launching Claude without --provider
    uses the saved choice to complete a file-reading task and exit normally.
    The Desktop profile keeps the dated provider ID and labels it Claude Haiku 4.5.
    """
    session = live_session
    task = FileTask(session)

    # Provider selection is interactive; --agents would bypass this real picker.
    command = [
        str(session.binary),
        "configure",
        "--profile",
        desktop_cli_profile,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    ]
    with ConfigureTerminal(session, "claude", command, "configure-provider") as configure:
        configure.select_agent("Claude Code")
        configure.choose("How should Claude Code get its models?", "External Models")
        configure.choose("Select a model provider service:", claude_provider)
        configure.finish(timeout=240)
    assert session.workspace_state()["provider_services"]["claude"] == claude_provider
    assert claude_provider in session.run("status").stdout
    assert_desktop_model_labels(
        session, {claude_provider_model: "Claude Haiku 4.5"}, provider=claude_provider
    )

    with AgentTerminal(
        session, "claude", [str(session.binary), "claude"], "provider-session"
    ) as tui:
        tui.boot()
        tui.submit(task.prompt)
        tui.wait_for_task(task, timeout=300)
        tui.exit_normally()
    task.assert_completed(session, "claude")
    session.assert_not_routed()


def test_ug_configure_claude_bedrock_mps(
    live_session, claude_bedrock_provider, desktop_cli_profile
):
    """Scenario: pick the real AWS Bedrock (amazon_bedrock) Anthropic MPS in the provider picker.
    This MPS allows only a restricted set of models (its explicit targets: Sonnet 5.5 + Opus 5.5), unlike
    an allow_all_targets service, so discovery must surface exactly that allow-list.

    Expected: ug saves that provider, and launching Claude without --provider completes a
    file-reading task. The launch tier preference (sonnet > opus > haiku) resolves to Sonnet 5.5.
    This is the live check that the real ug +
    Claude Code binary resolve a region-prefixed Bedrock slug (us.anthropic.claude-...) into a
    working launch -- the model-id pinning that unit tests only cover against mocked listings.
    The Desktop profile labels both regional targets as Claude Sonnet 5.5 / Claude Opus 5.5
    while preserving their exact routing IDs and the provider header.
    """
    session = live_session
    task = FileTask(session)

    command = [
        str(session.binary),
        "configure",
        "--profile",
        desktop_cli_profile,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    ]
    with ConfigureTerminal(session, "claude", command, "configure-bedrock-provider") as configure:
        configure.select_agent("Claude Code")
        configure.choose("How should Claude Code get its models?", "External Models")
        configure.choose("Select a model provider service:", claude_bedrock_provider)
        configure.finish(timeout=240)
    assert session.workspace_state()["provider_services"]["claude"] == claude_bedrock_provider
    assert claude_bedrock_provider in session.run("status").stdout
    assert_desktop_model_labels(
        session,
        {
            "us.anthropic.claude-sonnet-5-5": "Claude Sonnet 5.5",
            "us.anthropic.claude-opus-5-5": "Claude Opus 5.5",
        },
        provider=claude_bedrock_provider,
    )

    with AgentTerminal(
        session, "claude", [str(session.binary), "claude"], "bedrock-provider-session"
    ) as tui:
        tui.boot()
        tui.submit(task.prompt)
        tui.wait_for_task(task, timeout=300)
        tui.exit_normally()
    task.assert_completed(session, "claude")
    assert_completed_task_model(session, "claude", task.value, BEDROCK_SONNET_RESPONSE_MODEL)
    session.assert_not_routed()


@pytest.mark.usefixtures("unmanaged_workspace")
def test_ug_configure_claude_bedrock_mantle_mps(
    live_session, workspace, claude_bedrock_mantle_provider, claude_bedrock_mantle_model
):
    """Scenario: select the Bedrock Mantle MPS in ug configure, then launch Claude on Opus 5.5.

    Expected: ug saves the provider, and an interactive launch without --provider completes
    a file-reading task on the selected Mantle model, exits normally, and leaves routing off.
    """
    session = live_session
    task = FileTask(session)
    session.record("model.json", {"model": claude_bedrock_mantle_model})
    command = [
        str(session.binary),
        "configure",
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    ]
    with ConfigureTerminal(session, "claude", command, "configure-mantle-provider") as configure:
        configure.select_agent("Claude Code")
        configure.choose("How should Claude Code get its models?", "External Models")
        configure.choose("Select a model provider service:", claude_bedrock_mantle_provider)
        configure.finish(timeout=240)
    assert (
        session.workspace_state()["provider_services"]["claude"] == claude_bedrock_mantle_provider
    )
    assert claude_bedrock_mantle_provider in session.run("status").stdout
    settings = json.loads((session.home / ".claude/ucode-settings.json").read_text())
    assert (
        f"Databricks-Model-Provider-Service: {claude_bedrock_mantle_provider}"
        in settings["env"]["ANTHROPIC_CUSTOM_HEADERS"]
    )

    command = [str(session.binary), "claude", "--model", claude_bedrock_mantle_model]
    with AgentTerminal(session, "claude", command, "mantle-provider-session") as tui:
        tui.boot()
        tui.submit(task.prompt)
        tui.wait_for_task(task, timeout=300)
        tui.exit_normally()
    task.assert_completed(session, "claude")
    assert_completed_task_model(
        session, "claude", task.value, claude_bedrock_mantle_model.removeprefix("anthropic.")
    )
    session.assert_not_routed()


@pytest.mark.usefixtures("unmanaged_workspace")
def test_ug_configure_claude_foundry_mps(
    live_session,
    claude_foundry_provider,
    claude_foundry_provider_model,
    claude_foundry_provider_default_model,
    desktop_cli_profile,
):
    """Scenario: choose Foundry in ug configure, then launch plain ug claude.

    Expected: the provider appears in the real picker and is saved. Claude uses the
    saved provider and its allowed Sonnet deployment without an explicit model, completes
    a file-reading task in the TUI, and exits normally with smart routing disabled.
    Desktop labels both deployments as Claude Haiku 5.5 / Claude Sonnet 5.5 without changing IDs.
    """
    session = live_session
    task = FileTask(session)
    session.record("model.json", {"model": claude_foundry_provider_default_model})
    command = [
        str(session.binary),
        "configure",
        "--profile",
        desktop_cli_profile,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    ]
    with ConfigureTerminal(session, "claude", command, "configure-foundry-provider") as configure:
        configure.select_agent("Claude Code")
        configure.choose("How should Claude Code get its models?", "External Models")
        configure.choose("Select a model provider service:", claude_foundry_provider)
        configure.finish(timeout=240)
    assert session.workspace_state()["provider_services"]["claude"] == claude_foundry_provider
    assert claude_foundry_provider in session.run("status").stdout
    assert_desktop_model_labels(
        session,
        {
            claude_foundry_provider_model: "Claude Haiku 5.5",
            claude_foundry_provider_default_model: "Claude Sonnet 5.5",
        },
        provider=claude_foundry_provider,
    )
    settings = json.loads((session.home / ".claude/ucode-settings.json").read_text())
    assert settings["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == claude_foundry_provider_model
    assert (
        settings["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] == claude_foundry_provider_default_model
    )

    with AgentTerminal(
        session, "claude", [str(session.binary), "claude"], "foundry-provider-session"
    ) as tui:
        tui.boot()
        tui.submit(task.prompt)
        tui.wait_for_task(task, timeout=300)
        tui.exit_normally()
    task.assert_completed(session, "claude")
    assert_completed_task_model(
        session, "claude", task.value, claude_foundry_provider_default_model
    )
    session.assert_not_routed()


@pytest.mark.usefixtures("unmanaged_workspace")
def test_ug_configure_claude_custom_foundry_mps(
    live_session,
    claude_custom_provider,
    claude_custom_provider_model,
    desktop_cli_profile,
):
    """Scenario: select a custom Foundry MPS in ug configure, then launch plain ug claude.

    Expected: the real picker saves the provider and its Sonnet default. Claude uses that
    saved provider without launch overrides, reads a local file in the TUI, returns its
    unknown value on the expected deployment, and exits normally without smart routing.
    Desktop labels that deployment Claude Sonnet 5.5 and preserves the custom provider header.
    """
    session = live_session
    task = FileTask(session)
    session.record("model.json", {"model": claude_custom_provider_model})
    command = [
        str(session.binary),
        "configure",
        "--profile",
        desktop_cli_profile,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    ]
    with ConfigureTerminal(session, "claude", command, "configure-custom-provider") as configure:
        configure.select_agent("Claude Code")
        configure.choose("How should Claude Code get its models?", "External Models")
        configure.choose("Select a model provider service:", claude_custom_provider)
        configure.finish(timeout=240)
    assert session.workspace_state()["provider_services"]["claude"] == claude_custom_provider
    assert claude_custom_provider in session.run("status").stdout
    assert_desktop_model_labels(
        session,
        {claude_custom_provider_model: "Claude Sonnet 5.5"},
        provider=claude_custom_provider,
    )
    settings = json.loads((session.home / ".claude/ucode-settings.json").read_text())
    assert settings["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] == claude_custom_provider_model
    headers = settings["env"]["ANTHROPIC_CUSTOM_HEADERS"].splitlines()
    assert f"Databricks-Model-Provider-Service: {claude_custom_provider}" in headers

    with AgentTerminal(
        session, "claude", [str(session.binary), "claude"], "custom-provider-session"
    ) as tui:
        tui.boot()
        tui.submit(task.prompt)
        tui.wait_for_task(task, timeout=300)
        tui.exit_normally()
    task.assert_completed(session, "claude")
    assert_completed_task_model(session, "claude", task.value, claude_custom_provider_model)
    session.assert_not_routed()
