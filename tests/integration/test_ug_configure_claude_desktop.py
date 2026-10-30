"""Black-box lifecycle coverage for Claude Desktop profile configuration."""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import anyio
import pytest
from utils.constants import CLAUDE_RELAY_TEST_MODEL, CLAUDE_TEST_MODEL
from utils.desktop import (
    assert_desktop_mcp,
    desktop_config_library,
    installed_cowork_servers,
    prepare_cowork_home,
    system_desktop_marketplace,
)
from utils.harness import process_group_options, stop_process
from utils.provider_catalog import fetch_anthropic_parent_catalog
from utils.terminal import ConfigureTerminal, TerminalProcess

pytestmark = [pytest.mark.live, pytest.mark.claude]

UG_PROFILE_ID = "5b36a0b9-b56d-47cf-b6a4-0904e0bc14f6"
UNRELATED_PROFILE_ID = "62de45ce-d2ad-4507-a8e2-7e59529af303"
CLI_PROFILE = "desktop-integration"
UNRELATED_PROFILE = {"inferenceProvider": "anthropic", "userSetting": "keep"}
UNRELATED_PLUGIN_ID = "personal@desktop-integration"
UNRELATED_PLUGIN_MANIFEST = {"name": "personal", "version": "1.0.0"}


def _prepare_desktop(session, workspace):
    root = prepare_cowork_home(session)
    marketplace = session.cwd / "personal-marketplace"
    plugin = marketplace / "plugins/personal/.claude-plugin"
    plugin.mkdir(parents=True)
    (plugin / "plugin.json").write_text(json.dumps(UNRELATED_PLUGIN_MANIFEST))
    (marketplace / ".claude-plugin").mkdir()
    (marketplace / ".claude-plugin/marketplace.json").write_text(
        json.dumps(
            {
                "name": "desktop-integration",
                "owner": {"name": "Desktop integration"},
                "plugins": [{"name": "personal", "source": "./plugins/personal"}],
            }
        )
    )
    # Install user-owned input through Claude so its native registry format stays authoritative.
    claude_config_dir = session.env["CLAUDE_CONFIG_DIR"]
    session.env["CLAUDE_CONFIG_DIR"] = str(root)
    try:
        session.run(
            "plugin",
            "marketplace",
            "add",
            str(marketplace),
            "--cowork",
            "--json",
            binary=session.which("claude"),
        )
        session.run(
            "plugin",
            "install",
            UNRELATED_PLUGIN_ID,
            "--cowork",
            "--json",
            binary=session.which("claude"),
        )
    finally:
        session.env["CLAUDE_CONFIG_DIR"] = claude_config_dir
    installed = json.loads((root / "cowork_plugins/installed_plugins.json").read_text())
    unrelated_plugin = installed["plugins"][UNRELATED_PLUGIN_ID]
    assert unrelated_plugin
    config_library = desktop_config_library(session)
    config_library.mkdir(parents=True)
    (config_library / f"{UNRELATED_PROFILE_ID}.json").write_text(json.dumps(UNRELATED_PROFILE))
    (config_library / "_meta.json").write_text(
        json.dumps(
            {
                "entries": [{"id": UNRELATED_PROFILE_ID, "name": "Personal"}],
                "appliedId": UNRELATED_PROFILE_ID,
                "unrelatedMetadata": {"keep": True},
            }
        )
    )
    Path(session.env["DATABRICKS_CONFIG_FILE"]).write_text(
        f"[{CLI_PROFILE}]\nhost = {workspace}\nauth_type = databricks-cli\n"
    )
    return config_library, root, unrelated_plugin


def _assert_unrelated_cowork_plugin(root, expected):
    installed = json.loads((root / "cowork_plugins/installed_plugins.json").read_text())
    assert installed["plugins"][UNRELATED_PLUGIN_ID] == expected
    for entry in expected:
        manifest = Path(entry["installPath"]) / ".claude-plugin/plugin.json"
        assert json.loads(manifest.read_text()) == UNRELATED_PLUGIN_MANIFEST


def _desktop_profile(session, config_library, workspace):
    profile = json.loads((config_library / f"{UG_PROFILE_ID}.json").read_text())
    assert profile["inferenceProvider"] == "gateway"
    assert profile["inferenceGatewayBaseUrl"] == workspace + "/ai-gateway/anthropic"
    assert profile["inferenceGatewayAuthScheme"] == "bearer"
    assert profile["inferenceCredentialKind"] == "helper-script"
    helper = Path(profile["inferenceCredentialHelper"])
    assert helper.is_absolute() and helper.is_file()
    assert helper.parent == session.binary.parent and helper.name.lower().startswith("python")
    return profile


def _assert_desktop_selection(config_library, *, configured):
    entries = [{"id": UNRELATED_PROFILE_ID, "name": "Personal"}]
    if configured:
        entries.append({"id": UG_PROFILE_ID, "name": f"Unity Gateway - {CLI_PROFILE}"})
    assert json.loads((config_library / "_meta.json").read_text()) == {
        "entries": entries,
        "appliedId": UG_PROFILE_ID if configured else UNRELATED_PROFILE_ID,
        "unrelatedMetadata": {"keep": True},
    }
    assert json.loads((config_library / f"{UNRELATED_PROFILE_ID}.json").read_text()) == (
        UNRELATED_PROFILE
    )


def _revert_desktop(session, config_library, root, unrelated_plugin):
    if os.name == "posix":
        with TerminalProcess(
            session, "ug", [str(session.binary), "revert"], "desktop-revert"
        ) as terminal:
            terminal.finish()
    else:
        session.run("revert", timeout=120)
    assert not (config_library / f"{UG_PROFILE_ID}.json").exists()
    _assert_desktop_selection(config_library, configured=False)
    assert not (session.home / ".ucode/claude-desktop.json").exists()
    assert not (session.home / ".ucode/claude-cowork.json").exists()
    assert not (session.home / ".ucode/claude-cowork-marketplace").exists()
    system_source = system_desktop_marketplace()
    if system_source is not None:
        assert not system_source.exists()
    installed = root / "cowork_plugins/installed_plugins.json"
    if installed.exists():
        assert "ug@unity-gateway-cowork" not in json.loads(installed.read_text())["plugins"]
    marketplaces = root / "cowork_plugins/known_marketplaces.json"
    assert "unity-gateway-cowork" not in json.loads(marketplaces.read_text())
    _assert_unrelated_cowork_plugin(root, unrelated_plugin)


def _run_credential_helper(session, profile):
    # session.run records stdout before we can validate and redact an unexpected credential.
    process = subprocess.Popen(
        [profile["inferenceCredentialHelper"], *profile["inferenceCredentialHelperArgs"]],
        cwd=session.cwd,
        env=session.env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        **process_group_options(),
    )
    try:
        token_output, diagnostics = process.communicate(timeout=30)
    finally:
        stop_process(process)
    assert process.returncode == 0, session.redact(diagnostics)
    assert session.redact(diagnostics, strip_ansi=False) == ""
    session.record("desktop-credential-helper.json", {"exit_code": process.returncode})
    return token_output


def _assert_desktop_message(session, profile, model, headers, evidence_name):
    request = urllib.request.Request(
        profile["inferenceGatewayBaseUrl"] + "/v1/messages",
        data=json.dumps(
            {
                "model": model,
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "Reply with a short greeting."}],
            }
        ).encode(),
        headers={
            **profile["inferenceCustomHeaders"],
            **headers,
            "Anthropic-Version": "2023-06-01",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            assert response.status == 200
            message = json.load(response)
    except urllib.error.HTTPError as error:
        details = session.redact(error.read().decode("utf-8", errors="replace"))
        session.record(evidence_name, {"status": error.code, "error": details})
        raise AssertionError(f"Desktop Messages failed ({error.code}): {details}") from None
    session.record(
        evidence_name,
        {"status": response.status, "requested_model": model, "response": message},
    )
    assert message["type"] == "message" and message["role"] == "assistant"
    # A model-service alias can return its provider's model ID.
    assert isinstance(message["model"], str) and message["model"].strip()
    assert message["stop_reason"] == "end_turn"
    assert "".join(part["text"] for part in message["content"] if part["type"] == "text").strip()
    return message


def test_ug_configure_claude_desktop_profile_lifecycle(live_session, workspace):
    """Scenario: configure Desktop with system.ai models, use inference and MCPs, then revert.

    Expected: installed ug writes one complete fixed-UUID gateway profile, selects it without
    changing unrelated profile data, and later removes only its own profile while restoring the
    prior selection. The exact generated helper returns a credential accepted by the live models
    API and completes a Messages request on a configured system.ai Claude model. The installed
    Cowork plugin discovers web_search and finds Databricks' official website.
    Repeat configure preserves both configurations; an unrelated personal plugin survives
    configure and revert unchanged. This checks native Linux/Windows commands
    and protocols, not Desktop GUI, skill execution, or browser login; CI supplies a bearer.
    """
    session = live_session
    config_library, root, unrelated_plugin = _prepare_desktop(session, workspace)

    configure_args = (
        "configure",
        "--agents",
        "claude",
        "--profile",
        CLI_PROFILE,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )
    session.run(*configure_args, timeout=240)
    assert not session.workspace_state().get("provider_services", {}).get("claude")

    profile = _desktop_profile(session, config_library, workspace)
    assert profile["inferenceCredentialHelperArgs"] == [
        "-m",
        "ucode.cli",
        "claude-desktop-auth",
        "--host",
        workspace,
        "--profile",
        CLI_PROFILE,
    ]
    token_output = _run_credential_helper(session, profile)
    token_matches = token_output == session.env["DATABRICKS_BEARER"].strip() + "\n"
    assert token_matches, "Desktop helper must emit only the configured CI bearer"
    catalog = fetch_anthropic_parent_catalog(workspace, token_output.strip(), "system.ai")
    session.record("desktop-helper.json", {"model_count": len(catalog.model_ids)})

    models = profile["inferenceModels"]
    assert models and all(
        model["name"].startswith("system.ai.") and "claude-" in model["name"].lower()
        for model in models
    )
    assert {model["name"] for model in models} <= set(catalog.model_ids)
    headers = {key.casefold(): value for key, value in profile["inferenceCustomHeaders"].items()}
    assert headers["x-databricks-use-coding-agent-mode"] == "true"
    assert "databricks-model-provider-service" not in headers
    assert CLAUDE_TEST_MODEL in {model["name"] for model in models}
    _assert_desktop_message(
        session,
        profile,
        CLAUDE_TEST_MODEL,
        {"Authorization": "Bearer " + token_output.strip()},
        "desktop-system-ai-message.json",
    )

    _assert_desktop_selection(config_library, configured=True)
    session.run("mcp", "add", "--names", "system.ai.web_search", timeout=240)
    servers = installed_cowork_servers(session, root)
    _assert_unrelated_cowork_plugin(root, unrelated_plugin)
    anyio.run(assert_desktop_mcp, session, servers["web_search"], workspace)

    session.run(*configure_args, timeout=240)
    assert _desktop_profile(session, config_library, workspace) == profile
    assert installed_cowork_servers(session, root) == servers
    _assert_unrelated_cowork_plugin(root, unrelated_plugin)
    _assert_desktop_selection(config_library, configured=True)
    _revert_desktop(session, config_library, root, unrelated_plugin)


@pytest.mark.tui
def test_ug_configure_claude_desktop_subscription_relay(
    live_session, workspace, claude_relayed_provider, claude_oauth_token
):
    """Scenario: configure Desktop with relay MPS, use inference and MCPs, then revert.

    Expected: the profile selects relay mode and its installed helper returns both the Claude
    subscription token and Databricks gateway header. Those exact credentials and profile
    headers complete a real Anthropic Messages request on the configured subscription model.
    Its installed Cowork plugin discovers web_search and finds Databricks' official website.
    Reconfigure preserves the profile/plugin; revert restores unrelated settings
    and unregisters only UG's plugin, preserving an unrelated personal plugin. No Desktop GUI,
    connector skill, or browser OAuth is exercised.
    """
    session = live_session
    config_library, root, unrelated_plugin = _prepare_desktop(session, workspace)

    command = [
        str(session.binary),
        "configure",
        "--profile",
        CLI_PROFILE,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    ]
    with ConfigureTerminal(session, "claude", command, "configure-desktop-relay") as configure:
        configure.select_agent("Claude Code")
        configure.choose("How should Claude Code get its models?", "External Models")
        configure.choose("Select a model provider service:", claude_relayed_provider)
        configure.finish(timeout=240)

    profile = _desktop_profile(session, config_library, workspace)
    claude_binary = session.which("claude")
    helper_args = [
        "-m",
        "ucode.cli",
        "claude-desktop-auth",
        "--host",
        workspace,
        "--profile",
        CLI_PROFILE,
        "--relay",
        "--claude-binary",
        str(claude_binary),
    ]
    assert profile["inferenceCredentialHelperArgs"] == helper_args
    assert profile["inferenceCustomHeaders"]["Databricks-Model-Provider-Service"] == (
        claude_relayed_provider
    )
    assert profile["inferenceCustomHeaders"]["x-databricks-use-coding-agent-mode"] == "true"
    assert profile["inferenceModels"]

    if sys.platform == "darwin":
        session.env["CLAUDE_CODE_OAUTH_TOKEN"] = claude_oauth_token
    else:
        credentials_path = Path(session.env["CLAUDE_CONFIG_DIR"]) / ".credentials.json"
        credentials_path.parent.mkdir(parents=True, exist_ok=True)
        credentials_path.write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": claude_oauth_token,
                        "expiresAt": int((time.time() + 3600) * 1000),
                    }
                }
            )
        )

    credential = json.loads(_run_credential_helper(session, profile))
    credential_matches = credential == {
        "token": claude_oauth_token,
        "headers": {
            "X-Databricks-AI-Gateway-Token": "Bearer " + session.env["DATABRICKS_BEARER"].strip()
        },
    }
    assert credential_matches, (
        "Relay helper must return separate subscription and gateway credentials"
    )
    assert CLAUDE_RELAY_TEST_MODEL in {model["name"] for model in profile["inferenceModels"]}

    message = _assert_desktop_message(
        session,
        profile,
        CLAUDE_RELAY_TEST_MODEL,
        {**credential["headers"], "Authorization": "Bearer " + credential["token"]},
        "desktop-relay-message.json",
    )
    # This dated native relay ID must be returned unchanged; it is not a service alias.
    assert message["model"] == CLAUDE_RELAY_TEST_MODEL
    _assert_desktop_selection(config_library, configured=True)
    session.run("mcp", "add", "--names", "system.ai.web_search", timeout=240)
    servers = installed_cowork_servers(session, root)
    _assert_unrelated_cowork_plugin(root, unrelated_plugin)
    anyio.run(assert_desktop_mcp, session, servers["web_search"], workspace)

    with ConfigureTerminal(session, "claude", command, "reconfigure-desktop-relay") as configure:
        configure.select_agent("Claude Code")
        configure.choose("How should Claude Code get its models?", "External Models")
        configure.choose("Select a model provider service:", claude_relayed_provider)
        configure.finish(timeout=240)
    assert _desktop_profile(session, config_library, workspace) == profile
    assert installed_cowork_servers(session, root) == servers
    _assert_unrelated_cowork_plugin(root, unrelated_plugin)
    _assert_desktop_selection(config_library, configured=True)
    _revert_desktop(session, config_library, root, unrelated_plugin)
