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
from utils.desktop import desktop_config_library, installed_cowork_servers, prepare_cowork_home
from utils.evidence import fresh_id
from utils.harness import process_group_options, stop_process
from utils.mcp_fixture import MCP_FIXTURE_SERVICES, assert_mcp_fixture_call
from utils.terminal import ConfigureTerminal, TerminalProcess

pytestmark = [pytest.mark.live, pytest.mark.claude]

UG_PROFILE_ID = "5b36a0b9-b56d-47cf-b6a4-0904e0bc14f6"
UNRELATED_PROFILE_ID = "62de45ce-d2ad-4507-a8e2-7e59529af303"
CLI_PROFILE = "desktop-integration"


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
    marker = fresh_id()
    request = urllib.request.Request(
        profile["inferenceGatewayBaseUrl"] + "/v1/messages",
        data=json.dumps(
            {
                "model": model,
                "max_tokens": 32,
                "messages": [{"role": "user", "content": f"Reply with exactly: {marker}"}],
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
    session.record(evidence_name, {"marker": marker, "response": message})
    assert message["type"] == "message" and message["role"] == "assistant"
    assert message["model"] == model
    assert message["stop_reason"] == "end_turn"
    assert (
        "".join(part["text"] for part in message["content"] if part["type"] == "text").strip()
        == marker
    )


def test_ug_configure_claude_desktop_profile_lifecycle(live_session, workspace):
    """Scenario: configure twice beside an unrelated Desktop profile, then revert.

    Expected: installed ug writes one complete fixed-UUID gateway profile, selects it without
    changing unrelated profile data, and later removes only its own profile while restoring the
    prior selection. The exact generated helper command returns a credential accepted by the live models API.
    This checks native Linux/Windows file handling and helper execution, not Desktop GUI
    or inference; authentication uses the supported CI bearer override.
    """
    session = live_session
    config_library = desktop_config_library(session)
    config_library.mkdir(parents=True)
    unrelated_profile_path = config_library / f"{UNRELATED_PROFILE_ID}.json"
    unrelated_profile = {"inferenceProvider": "anthropic", "userSetting": "keep"}
    unrelated_profile_path.write_text(json.dumps(unrelated_profile, indent=2))
    metadata_path = config_library / "_meta.json"
    metadata_path.write_text(
        json.dumps(
            {
                "entries": [{"id": UNRELATED_PROFILE_ID, "name": "Personal"}],
                "appliedId": UNRELATED_PROFILE_ID,
                "unrelatedMetadata": {"keep": True},
            },
            indent=2,
        )
    )
    Path(session.env["DATABRICKS_CONFIG_FILE"]).write_text(
        f"[{CLI_PROFILE}]\nhost = {workspace}\nauth_type = databricks-cli\n"
    )

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

    ug_profile_path = config_library / f"{UG_PROFILE_ID}.json"
    profile = json.loads(ug_profile_path.read_text())
    assert profile["inferenceProvider"] == "gateway"
    assert profile["inferenceGatewayBaseUrl"] == workspace + "/ai-gateway/anthropic"
    assert profile["inferenceGatewayAuthScheme"] == "bearer"
    assert profile["inferenceCredentialKind"] == "helper-script"
    helper = Path(profile["inferenceCredentialHelper"])
    assert helper.is_absolute() and helper.is_file()
    assert helper.parent == session.binary.parent and helper.name.lower().startswith("python")
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
    request = urllib.request.Request(
        profile["inferenceGatewayBaseUrl"] + "/v1/models",
        headers={
            **profile["inferenceCustomHeaders"],
            "Authorization": "Bearer " + token_output.strip(),
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        assert response.status == 200
        catalog = json.load(response)["data"]
        assert catalog
        session.record(
            "desktop-helper.json", {"models_status": response.status, "model_count": len(catalog)}
        )

    models = profile["inferenceModels"]
    assert models and all("claude-" in model["name"].lower() for model in models)
    assert profile["inferenceCustomHeaders"]["x-databricks-use-coding-agent-mode"] == "true"

    metadata = json.loads(metadata_path.read_text())
    assert metadata == {
        "entries": [
            {"id": UNRELATED_PROFILE_ID, "name": "Personal"},
            {"id": UG_PROFILE_ID, "name": f"Unity Gateway - {CLI_PROFILE}"},
        ],
        "appliedId": UG_PROFILE_ID,
        "unrelatedMetadata": {"keep": True},
    }
    assert json.loads(unrelated_profile_path.read_text()) == unrelated_profile

    session.run(*configure_args, timeout=240)
    assert json.loads(ug_profile_path.read_text()) == profile
    metadata = json.loads(metadata_path.read_text())
    assert [entry["id"] for entry in metadata["entries"]].count(UG_PROFILE_ID) == 1
    assert json.loads(unrelated_profile_path.read_text()) == unrelated_profile

    if os.name == "posix":
        with TerminalProcess(
            session, "ug", [str(session.binary), "revert"], "desktop-revert"
        ) as terminal:
            terminal.finish()
    else:
        session.run("revert", timeout=120)

    assert not ug_profile_path.exists()
    assert json.loads(metadata_path.read_text()) == {
        "entries": [{"id": UNRELATED_PROFILE_ID, "name": "Personal"}],
        "appliedId": UNRELATED_PROFILE_ID,
        "unrelatedMetadata": {"keep": True},
    }
    assert json.loads(unrelated_profile_path.read_text()) == unrelated_profile
    assert not (session.home / ".ucode/claude-desktop.json").exists()


@pytest.mark.tui
def test_ug_configure_claude_desktop_subscription_relay(
    live_session, workspace, claude_relayed_provider, claude_oauth_token, claude_provider_model
):
    """Scenario: configure a relayed Desktop profile using live provider credentials.

    Expected: the profile selects relay mode and its installed helper returns both the Claude
    subscription token and Databricks gateway header. Those exact credentials and profile
    headers complete a real Anthropic Messages request with a fresh marker on the configured
    subscription model. This does not launch the Desktop GUI or run browser OAuth login.
    """
    session = live_session
    config_library = desktop_config_library(session)
    config_library.mkdir(parents=True)
    Path(session.env["DATABRICKS_CONFIG_FILE"]).write_text(
        f"[{CLI_PROFILE}]\nhost = {workspace}\nauth_type = databricks-cli\n"
    )

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

    profile = json.loads((config_library / f"{UG_PROFILE_ID}.json").read_text())
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
    assert claude_provider_model in {model["name"] for model in profile["inferenceModels"]}

    _assert_desktop_message(
        session,
        profile,
        claude_provider_model,
        {**credential["headers"], "Authorization": "Bearer " + credential["token"]},
        "desktop-relay-message.json",
    )


@pytest.mark.tui
def test_ug_configure_claude_desktop_anthropic_mps(
    live_session, workspace, claude_provider, claude_provider_model
):
    """Scenario: select the real non-relayed Anthropic MPS and use its Desktop profile.

    Expected: the profile carries the selected provider and its model, the generated helper
    returns only the Databricks bearer, and those exact settings complete a real Messages
    request with a fresh marker. This covers Desktop's protocol boundary, not its GUI.
    """
    session = live_session
    config_library = desktop_config_library(session)
    config_library.mkdir(parents=True)
    Path(session.env["DATABRICKS_CONFIG_FILE"]).write_text(
        f"[{CLI_PROFILE}]\nhost = {workspace}\nauth_type = databricks-cli\n"
    )
    command = [
        str(session.binary),
        "configure",
        "--profile",
        CLI_PROFILE,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    ]
    with ConfigureTerminal(session, "claude", command, "configure-desktop-mps") as configure:
        configure.select_agent("Claude Code")
        configure.choose("How should Claude Code get its models?", "External Models")
        configure.choose("Select a model provider service:", claude_provider)
        configure.finish(timeout=240)

    profile = json.loads((config_library / f"{UG_PROFILE_ID}.json").read_text())
    assert profile["inferenceGatewayBaseUrl"] == workspace + "/ai-gateway/anthropic"
    assert profile["inferenceGatewayAuthScheme"] == "bearer"
    assert profile["inferenceCustomHeaders"]["Databricks-Model-Provider-Service"] == claude_provider
    assert claude_provider_model in {model["name"] for model in profile["inferenceModels"]}
    assert "--relay" not in profile["inferenceCredentialHelperArgs"]
    token_output = _run_credential_helper(session, profile)
    token_matches = token_output == session.env["DATABRICKS_BEARER"].strip() + "\n"
    assert token_matches, "Non-relayed helper must emit only the Databricks bearer"

    _assert_desktop_message(
        session,
        profile,
        claude_provider_model,
        {"Authorization": "Bearer " + token_output.strip()},
        "desktop-mps-message.json",
    )


@pytest.mark.desktop_mcp
def test_ug_configure_claude_desktop_cowork_mcp(live_session, workspace):
    """Scenario: configure Cowork beside an existing Desktop account in the MCP fixture workspace.

    Expected: the public configure command installs the Cowork plugin through the real Claude
    CLI. Commands read from that installed plugin discover
    and call both live MCP fixtures with fresh-run receipts. This does not launch the Desktop GUI,
    execute its discovery skill, or run browser OAuth consent.
    """
    session = live_session
    root = prepare_cowork_home(session)
    Path(session.env["DATABRICKS_CONFIG_FILE"]).write_text(
        f"[{CLI_PROFILE}]\nhost = {workspace}\nauth_type = databricks-cli\n"
    )
    session.run(
        "configure",
        "--agents",
        "claude",
        "--profile",
        CLI_PROFILE,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )
    servers = installed_cowork_servers(session, root)
    run_id = fresh_id()

    async def scenario():
        for service, tool in MCP_FIXTURE_SERVICES.items():
            alias = service.rsplit(".", 1)[1]
            await assert_mcp_fixture_call(session, servers[alias], tool, run_id)

    anyio.run(scenario)
