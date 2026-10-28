"""Claude Desktop profile assertions for installed-CLI CUJs."""

from __future__ import annotations

import json
from pathlib import Path

PROFILE_ID = "5b36a0b9-b56d-47cf-b6a4-0904e0bc14f6"
CLI_PROFILE = "cuj-managed"


def enable_claude_desktop(session, workspace: str) -> Path:
    desktop_root = Path(session.env["XDG_CONFIG_HOME"]) / "Claude-3p"
    desktop_root.mkdir(parents=True)
    Path(session.env["DATABRICKS_CONFIG_FILE"]).write_text(
        f"[{CLI_PROFILE}]\nhost = {workspace}\nauth_type = databricks-cli\n",
        encoding="utf-8",
    )
    return desktop_root / "configLibrary"


def assert_claude_desktop_profile(
    session,
    config_library: Path,
    workspace: str,
    *,
    models: list[dict[str, str]],
    custom_headers: dict[str, str],
    otel_enabled: bool = False,
) -> None:
    profile_path = config_library / f"{PROFILE_ID}.json"
    assert profile_path.is_file(), profile_path
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    helper = Path(profile["inferenceCredentialHelper"])
    assert helper.is_absolute() and helper.is_file(), helper
    assert helper.parent == session.binary.parent and helper.name.startswith("python"), helper
    profile["inferenceCustomHeaders"] = {
        name.casefold(): value for name, value in profile["inferenceCustomHeaders"].items()
    }
    expected = {
        "inferenceProvider": "gateway",
        "inferenceGatewayBaseUrl": workspace + "/ai-gateway/anthropic",
        "inferenceGatewayAuthScheme": "bearer",
        "inferenceCredentialKind": "helper-script",
        "inferenceCredentialHelper": str(helper),
        "inferenceCredentialHelperArgs": [
            "-m",
            "ucode.cli",
            "claude-desktop-auth",
            "--host",
            workspace,
            "--profile",
            CLI_PROFILE,
        ],
        "inferenceCredentialHelperTtlSec": 900,
        "inferenceCredentialHelperTimeoutSec": 300,
        "inferenceCredentialHelperSilentRefreshEnabled": True,
        "modelDiscoveryEnabled": False,
        "inferenceModels": models,
        "inferenceCustomHeaders": custom_headers,
    }
    if otel_enabled:
        expected.update(
            otlpEndpoint=workspace + "/ai-gateway/otel",
            otlpProtocol="http/protobuf",
            otlpHeadersHelper=str(session.home / ".ucode/claude-desktop-otel-headers.sh"),
            otlpTracesEnabled=True,
        )
    session.record("desktop-profile.json", {"actual": profile, "expected": expected})
    assert profile == expected, (profile, expected)

    metadata = json.loads((config_library / "_meta.json").read_text(encoding="utf-8"))
    assert metadata == {
        "entries": [{"id": PROFILE_ID, "name": f"Unity Gateway - {CLI_PROFILE}"}],
        "appliedId": PROFILE_ID,
    }, metadata
