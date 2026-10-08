"""Managed-config CUJs for agent model lists, smart routing, and Codex catalog fallback metadata.

Every case injects a checked-in JSON CodingAgentConfig (tests/fixtures/managed_config/) via
UCODE_MANAGED_CONFIG_STUB on the managed workspace; only that config input is stubbed. See
tests/AGENTS.md rule 4.
"""

import json

import pytest
from utils.constants import MANAGED_CLAUDE_PROVIDER_SERVICE
from utils.evidence import FileTask
from utils.managed import use_managed_config_fixture
from utils.provider_catalog import fetch_anthropic_parent_catalog, fetch_anthropic_provider_catalog
from utils.terminal import AgentTerminal, TerminalProcess

# The picker fixture lists sonnet-5 but not haiku-4-5, which the managed workspace's own list has.
LIVE_ONLY = "haiku-4-5"
CODEX_DEFAULT = "system.ai.gpt-5-6-sol"
CODEX_WITHOUT_BUNDLED_METADATA = "system.ai.gpt-99"
MANAGED_CLAUDE_DEFAULT_ENV_KEYS = {
    "default_fable_model": "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "default_opus_model": "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "default_sonnet_model": "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "default_haiku_model": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
}

SMART_ROUTING_BANNER = "Using Unity Gateway Smart Router."


@pytest.mark.managed_fixture
@pytest.mark.claude
def test_managed_claude_mps_defaults_accompany_discovery(live_session, workspace):
    """Scenario: launch Claude under an injected config with defaults and MPS discovery.

    Expected: the installed ug launch writes the MPS header and every admin-authored default to
    both Claude settings files without changing the model ids, and replaces built-in picker rows
    with labeled family-default shortcuts followed by the independently fetched MPS catalog,
    including targets also used as defaults. This settings reconciliation check does not claim
    model inference.
    """
    session = live_session
    defaults = {
        "default_model": "anthropic.claude-sonnet-5",
        "default_fable_model": "anthropic.claude-fable-5-1",
        "default_opus_model": "anthropic.claude-opus-5",
        "default_sonnet_model": "anthropic.claude-sonnet-5",
        "default_haiku_model": "anthropic.claude-haiku-4-5",
    }
    use_managed_config_fixture(session, "claude_mps_defaults")
    result = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    assert "Select coding agents to configure:" not in result.stdout, result.stdout

    catalog = fetch_anthropic_provider_catalog(
        workspace, session.env["DATABRICKS_BEARER"], MANAGED_CLAUDE_PROVIDER_SERVICE
    )
    expected_families = ("opus", "sonnet", "haiku", "fable")
    command = [str(session.binary), "claude", "--", "--version"]
    with TerminalProcess(session, "claude", command, "managed-defaults-mps") as terminal:
        terminal.finish(timeout=240)

    private_settings = json.loads((session.home / ".claude" / "ucode-settings.json").read_text())
    os_managed_settings = json.loads(
        session.run("/etc/claude-code/managed-settings.json", binary="cat", timeout=30).stdout
    )
    for settings in (private_settings, os_managed_settings):
        env = settings.get("env") or {}
        expected_header = f"Databricks-Model-Provider-Service: {MANAGED_CLAUDE_PROVIDER_SERVICE}"
        assert expected_header in env.get("ANTHROPIC_CUSTOM_HEADERS", "").splitlines(), settings
        assert env.get("ANTHROPIC_MODEL") == defaults["default_model"], settings
        for config_key, env_key in MANAGED_CLAUDE_DEFAULT_ENV_KEYS.items():
            assert env.get(env_key) == defaults[config_key], settings
        picker = settings["modelPicker"]
        assert picker["replaceBuiltInOptions"] is True, picker
        default_options = picker["options"][: len(expected_families)]
        catalog_options = picker["options"][len(expected_families) :]
        for family, option in zip(expected_families, default_options, strict=True):
            assert option["model"] == family, option
            assert option["label"] == f"Default {family.title()}", option
            assert option["description"] == defaults[f"default_{family}_model"], option
        assert sorted(option["model"] for option in catalog_options) == sorted(catalog.model_ids)
        for option in catalog_options:
            if display_name := catalog.display_names.get(option["model"]):
                assert option["label"] == display_name, option


@pytest.mark.managed_fixture
@pytest.mark.claude
def test_managed_claude_parent_schema_defaults_accompany_discovery(live_session, workspace):
    """Scenario: launch Claude under an injected config with defaults and `system.ai` UC discovery.

    Expected: the installed ug launch writes the parent-schema header and every admin-authored
    default to both Claude settings files, adding ``[1m]`` only to Opus and Sonnet family defaults.
    The replacement picker contains those family defaults and every model independently fetched
    from the schema, preserving catalog labels. This settings reconciliation check does not claim
    model inference.
    """
    session = live_session
    parent_schema = "system.ai"
    defaults = {
        "default_model": f"{parent_schema}.claude-sonnet-5",
        "default_fable_model": f"{parent_schema}.claude-fable-5-1",
        "default_opus_model": f"{parent_schema}.claude-opus-5",
        "default_sonnet_model": f"{parent_schema}.claude-sonnet-5",
        "default_haiku_model": f"{parent_schema}.claude-haiku-4-5",
    }
    use_managed_config_fixture(session, "claude_parent_schema_defaults")
    result = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    assert "Select coding agents to configure:" not in result.stdout, result.stdout

    catalog = fetch_anthropic_parent_catalog(
        workspace, session.env["DATABRICKS_BEARER"], parent_schema
    )
    command = [str(session.binary), "claude", "--", "--version"]
    with TerminalProcess(session, "claude", command, "managed-defaults-parent-schema") as terminal:
        terminal.finish(timeout=240)

    private_settings = json.loads((session.home / ".claude" / "ucode-settings.json").read_text())
    os_managed_settings = json.loads(
        session.run("/etc/claude-code/managed-settings.json", binary="cat", timeout=30).stdout
    )
    for settings in (private_settings, os_managed_settings):
        env = settings.get("env") or {}
        expected_header = f"Databricks-Model-Service-Parent-Schema: {parent_schema}"
        assert expected_header in env.get("ANTHROPIC_CUSTOM_HEADERS", "").splitlines(), settings
        assert env.get("ANTHROPIC_MODEL") == defaults["default_model"], settings
        expected_picker_models = []
        for config_key, env_key in MANAGED_CLAUDE_DEFAULT_ENV_KEYS.items():
            expected = defaults[config_key]
            if config_key in {"default_opus_model", "default_sonnet_model"}:
                expected += "[1m]"
            assert env.get(env_key) == expected, settings
            expected_picker_models.append(
                defaults[config_key]
                if defaults[config_key] == defaults["default_model"]
                else expected
            )
        picker = settings["modelPicker"]
        assert picker["replaceBuiltInOptions"] is True, picker
        expected_picker_models = set(expected_picker_models) | set(catalog.model_ids)
        assert sorted(option["model"] for option in picker["options"]) == sorted(
            expected_picker_models
        ), picker
        for option in picker["options"]:
            if display_name := catalog.display_names.get(option["model"]):
                assert option["label"] == display_name, option


@pytest.mark.managed_fixture
@pytest.mark.claude
def test_managed_fixture_claude_model_picker_reflects_the_config(live_session, workspace):
    """Scenario: launch Claude under an injected managed config and open the /model picker.

    Expected: the picker offers the injected models (including one the live workspace does not
    publish) and omits a model the live workspace does publish.
    """
    session = live_session
    use_managed_config_fixture(session, "claude_model_picker")
    result = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    assert "Select coding agents to configure:" not in result.stdout, result.stdout

    with AgentTerminal(session, "claude", [str(session.binary), "claude"], "managed-model") as tui:
        tui.boot()
        tui.send("/model", "type the /model command")
        tui.send("\r", "open the model picker")
        tui.wait_for(
            lambda s: "sonnet-5" in s, "the /model picker to list the injected model", timeout=60
        )
        assert LIVE_ONLY not in tui.visible, tui.visible


@pytest.mark.managed_fixture
@pytest.mark.codex
def test_ug_configure_managed_codex_catalog_fallback(live_session, workspace):
    """Scenario: configure Codex from an injected model list.

    Expected: ug creates conservative fallback metadata for the unknown model, warns how to get
    richer metadata, and the real Codex TUI lists that model in its /model picker.
    """
    session = live_session
    use_managed_config_fixture(session, "codex_catalog_fallback")
    result = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    assert "Select coding agents to configure:" not in result.stdout, result.stdout
    assert "Codex is missing metadata for managed GPT model" in result.stdout, result.stdout
    assert CODEX_WITHOUT_BUNDLED_METADATA in result.stdout, result.stdout
    assert "`ug codex update`" in result.stdout, result.stdout

    catalog = json.loads((session.home / ".ucode" / "codex-model-catalog.json").read_text())
    models = catalog.get("models", [])
    listed = [model.get("slug") for model in models if model.get("visibility") == "list"]
    assert listed == [CODEX_DEFAULT, CODEX_WITHOUT_BUNDLED_METADATA], catalog
    fallback = next(
        model for model in models if model.get("slug") == CODEX_WITHOUT_BUNDLED_METADATA
    )
    assert fallback.get("tool_mode") is None, fallback
    assert fallback.get("input_modalities") == ["text"], fallback
    assert fallback.get("context_window") == 32768, fallback
    assert fallback.get("default_reasoning_level") == "none", fallback

    with AgentTerminal(
        session,
        "codex",
        [str(session.binary), "codex"],
        "managed-fallback",
    ) as tui:
        tui.boot()
        tui.submit("/model")
        tui.wait_for(
            lambda s: "gpt-99" in s,
            "the /model picker to list the injected custom-catalog model",
            timeout=60,
        )
        tui.send("\x1b", "close the model picker")
        tui.wait_for(lambda s: "Select Model and Effort" not in s, "the model picker to close")
        tui.exit_normally()


@pytest.mark.managed_fixture
@pytest.mark.claude
@pytest.mark.parametrize(
    "SMART_ROUTER_CONFIG_VERSION",
    [None, "first_prompt_and_subagent_no_orch_v0"],
    ids=["managed-default", "first_prompt_and_subagent_no_orch_v0"],
)
def test_managed_fixture_claude_smart_routing_banner(
    live_session, workspace, SMART_ROUTER_CONFIG_VERSION
):
    """Scenario: an admin config lists Claude models and launch uses its default or the
    customer first-prompt-and-subagent selector.

    Expected: `ug configure` applies the config without the personal agent selector, and both
    launch modes route the first real prompt: the TUI shows the Unity Gateway Smart Router
    banner naming the selected model, the routed answer completes the file task, and the session
    exits normally.
    """
    session = live_session
    task = FileTask(session)
    use_managed_config_fixture(session, "claude_smart_routing")
    result = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    assert "Select coding agents to configure:" not in result.stdout, result.stdout
    if SMART_ROUTER_CONFIG_VERSION is not None:
        session.env["SMART_ROUTER_CONFIG_VERSION"] = SMART_ROUTER_CONFIG_VERSION

    with AgentTerminal(
        session,
        "claude",
        [str(session.binary), "claude"],
        f"managed-smart-routing-{SMART_ROUTER_CONFIG_VERSION or 'managed-default'}",
    ) as tui:
        tui.boot()
        tui.submit(task.prompt)
        tui.wait_for(
            lambda screen: SMART_ROUTING_BANNER in screen,
            "the smart routing banner for the routed first prompt",
            timeout=120,
        )
        assert "Selected Model" in tui.visible, tui.visible
        tui.wait_for_task(task)
        tui.exit_normally()
    task.assert_completed(session, "claude")


@pytest.mark.managed_fixture
@pytest.mark.codex
@pytest.mark.parametrize(
    "SMART_ROUTER_CONFIG_VERSION",
    [None, "first_prompt_and_subagent_no_orch_v0"],
    ids=["managed-default", "first_prompt_and_subagent_no_orch_v0"],
)
def test_managed_fixture_codex_smart_routing_banner(
    live_session, workspace, SMART_ROUTER_CONFIG_VERSION
):
    """Scenario: an admin config lists Codex models and launch uses its default or the
    customer first-prompt-and-subagent selector.

    Expected: `ug configure` applies the config without the personal agent selector, and both
    launch modes route the first real prompt: the TUI shows the Unity Gateway Smart Router
    banner naming the selected model, the routed answer completes the file task, and the session
    exits normally.
    """
    session = live_session
    task = FileTask(session)
    use_managed_config_fixture(session, "codex_smart_routing")
    result = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    assert "Select coding agents to configure:" not in result.stdout, result.stdout
    if SMART_ROUTER_CONFIG_VERSION is not None:
        session.env["SMART_ROUTER_CONFIG_VERSION"] = SMART_ROUTER_CONFIG_VERSION

    with AgentTerminal(
        session,
        "codex",
        [str(session.binary), "codex"],
        f"managed-smart-routing-{SMART_ROUTER_CONFIG_VERSION or 'managed-default'}",
    ) as tui:
        tui.boot()
        tui.submit(task.prompt)
        tui.wait_for(
            lambda screen: SMART_ROUTING_BANNER in screen,
            "the smart routing banner for the routed first prompt",
            timeout=120,
        )
        assert "Selected Model" in tui.visible, tui.visible
        tui.wait_for_task(task)
        tui.exit_normally()
    task.assert_completed(session, "codex")
