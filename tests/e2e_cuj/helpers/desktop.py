"""Claude Desktop profile assertions for installed-CLI CUJs."""

from __future__ import annotations

import json
from pathlib import Path

PROFILE_ID = "5b36a0b9-b56d-47cf-b6a4-0904e0bc14f6"
CLI_PROFILE = "cuj-managed"
PERSONAL_PROFILE_ID = "62de45ce-d2ad-4507-a8e2-7e59529af303"
PERSONAL_DESKTOP_FILES = {
    f"{PERSONAL_PROFILE_ID}.json": json.dumps(
        {"inferenceProvider": "anthropic", "userSetting": "keep"}
    ).encode(),
    "_meta.json": json.dumps(
        {
            "entries": [{"id": PERSONAL_PROFILE_ID, "name": "Personal"}],
            "appliedId": PERSONAL_PROFILE_ID,
        }
    ).encode(),
}


def enable_claude_desktop(session, workspace: str) -> Path:
    desktop_root = Path(session.env["XDG_CONFIG_HOME"]) / "Claude-3p"
    desktop_root.mkdir(parents=True)
    Path(session.env["DATABRICKS_CONFIG_FILE"]).write_text(
        f"[{CLI_PROFILE}]\nhost = {workspace}\nauth_type = databricks-cli\n",
        encoding="utf-8",
    )
    config_library = desktop_root / "configLibrary"
    config_library.mkdir()
    for name, contents in PERSONAL_DESKTOP_FILES.items():
        (config_library / name).write_bytes(contents)
    return config_library


def assert_claude_desktop_unchanged(session, config_library: Path) -> None:
    assert {
        path.name: path.read_bytes() for path in config_library.iterdir()
    } == PERSONAL_DESKTOP_FILES
    assert not (session.home / ".ucode/claude-desktop.json").exists()


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
        "entries": [
            {"id": PERSONAL_PROFILE_ID, "name": "Personal"},
            {"id": PROFILE_ID, "name": f"Unity Gateway - {CLI_PROFILE}"},
        ],
        "appliedId": PROFILE_ID,
    }, metadata
    personal_name = f"{PERSONAL_PROFILE_ID}.json"
    assert (config_library / personal_name).read_bytes() == PERSONAL_DESKTOP_FILES[personal_name]
    assert {path.name for path in config_library.iterdir()} == {
        *PERSONAL_DESKTOP_FILES,
        f"{PROFILE_ID}.json",
    }
