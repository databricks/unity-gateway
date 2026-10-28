"""Black-box lifecycle coverage for Claude Desktop profile configuration."""

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from utils.harness import process_group_options, stop_process
from utils.terminal import ConfigureTerminal, TerminalProcess

pytestmark = [pytest.mark.live, pytest.mark.claude]

UG_PROFILE_ID = "5b36a0b9-b56d-47cf-b6a4-0904e0bc14f6"
UNRELATED_PROFILE_ID = "62de45ce-d2ad-4507-a8e2-7e59529af303"
CLI_PROFILE = "desktop-integration"


def _config_library(session) -> Path:
    if os.name == "nt":
        return Path(session.env["LOCALAPPDATA"]) / "Claude-3p/configLibrary"
    if sys.platform == "darwin":
        return session.home / "Library/Application Support/Claude-3p/configLibrary"
    return Path(session.env["XDG_CONFIG_HOME"]) / "Claude-3p/configLibrary"


def test_ug_configure_claude_desktop_profile_lifecycle(live_session, workspace):
    """Scenario: configure twice beside an unrelated Desktop profile, then revert.

    Expected: installed ug writes one complete fixed-UUID gateway profile, selects it without
    changing unrelated profile data, and later removes only its own profile while restoring the
    prior selection. The exact generated helper command returns a credential accepted by the live models API.
    This checks native Linux/Windows file handling and helper execution, not Desktop GUI
    or inference; authentication uses the supported CI bearer override.
    """
    session = live_session
    config_library = _config_library(session)
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
    # session.run records stdout before we can validate and redact an unexpected credential.
    process = subprocess.Popen(
        [str(helper), *profile["inferenceCredentialHelperArgs"]],
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
    token_matches = token_output == session.env["DATABRICKS_BEARER"].strip() + "\n"
    assert token_matches, "Desktop helper must emit only the configured CI bearer"
    assert session.redact(diagnostics, strip_ansi=False) == ""
    request = urllib.request.Request(
        profile["inferenceGatewayBaseUrl"] + "/v1/models",
        headers={
            **profile["inferenceCustomHeaders"],
            "Authorization": "Bearer " + token_output.strip(),
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        assert response.status == 200
        assert json.load(response)["data"]
    session.record("desktop-helper.json", {"exit_code": process.returncode, "models_status": 200})

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
    live_session, workspace, claude_relayed_provider, claude_oauth_token
):
    """Scenario: configure a relayed Desktop profile using live provider credentials.

    Expected: the profile selects relay mode and its installed helper returns both the Claude
    subscription token and Databricks gateway header. This does not launch the Desktop GUI.
    """
    session = live_session
    config_library = _config_library(session)
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

    helper = Path(profile["inferenceCredentialHelper"])
    result = session.run(*helper_args, binary=helper, timeout=30)
    assert json.loads(result.stdout) == {
        "token": "<redacted>",
        "headers": {"X-Databricks-AI-Gateway-Token": "Bearer <redacted>"},
    }
    assert result.stderr == ""
