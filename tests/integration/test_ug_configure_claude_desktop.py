"""Black-box lifecycle coverage for Claude Desktop profile configuration."""

import json
import os
import sys
from pathlib import Path

import pytest
from utils.terminal import TerminalProcess

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
    prior selection. This checks native Linux/Windows file handling, not Desktop GUI or inference.
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
