"""Compatible-model discovery and explicit selection through installed OpenCode."""

import json
import re
import uuid

import pytest
from utils.evidence import FileTask, assert_opencode_answer, opencode_completed_session

pytestmark = [pytest.mark.live, pytest.mark.opencode]


def test_ug_opencode_discovers_and_selects_compatible_model(
    live_session, unmanaged_workspace, opencode_model
):
    """Scenario: configure OpenCode, list a compatible model, then select it twice.

    Expected: the native model list includes the API-compatible model without a
    manual config edit. Each headless run reads and edits a file, completes an
    assistant answer under that model, and retains discovery and SDK metadata.
    This does not cover TUI use.
    """
    session = live_session
    session.run(
        "configure",
        "--agents",
        "opencode",
        "--workspace",
        unmanaged_workspace,
        "--disable-databricks-ai-tools",
        timeout=240,
    )
    before = session.workspace_state()
    discovered = before["opencode_models"]
    assert discovered, "ug configure found no OpenCode models"
    matching_buckets = [
        bucket for bucket in ("openai", "oss") if opencode_model in discovered.get(bucket, [])
    ]
    assert len(matching_buckets) == 1, (
        "--opencode-model must advertise a supported MLflow generation API and appear in exactly "
        f"one OpenCode provider bucket (got {matching_buckets!r})"
    )
    bucket = matching_buckets[0]
    provider_id = f"databricks-{bucket}"
    api_types = before["opencode_model_api_types"][opencode_model]
    assert {
        "mlflow/v1/responses",
        "mlflow/v1/chat/completions",
    }.intersection(api_types), api_types
    expected_sdk = (
        "@ai-sdk/openai" if "mlflow/v1/responses" in api_types else "@ai-sdk/openai-compatible"
    )
    discovery_and_defaults = {
        key: value
        for key, value in before.items()
        if key.endswith("_models")
        or key.endswith("_default_model")
        or key == "opencode_model_api_types"
    }
    config_path = session.home / ".ucode/opencode-xdg/opencode/opencode.json"
    configured = json.loads(config_path.read_text())
    selector = f"{provider_id}/{opencode_model}"
    configured_model = configured["provider"][provider_id]["models"][opencode_model]
    assert configured_model["provider"]["npm"] == expected_sdk
    previous_xdg = session.env["XDG_CONFIG_HOME"]
    session.env["XDG_CONFIG_HOME"] = str(config_path.parents[1])
    try:
        native_models = session.run("models", provider_id, binary="opencode", timeout=60)
    finally:
        session.env["XDG_CONFIG_HOME"] = previous_xdg
    assert selector in native_models.stdout.splitlines(), native_models.stdout
    session.record(
        "model.json",
        {
            "model": opencode_model,
            "selector": selector,
            "source": "compatible model-service discovery",
            "discovered": discovered,
            "supported_api_types": api_types,
        },
    )

    sessions = set()
    for launch in ("first", "repeat"):
        task = FileTask(session)
        expected = task.value + "\ndone\n"
        prompt = (
            f"Read {task.filename} using a tool. Append a line containing exactly done to it. "
            "Read the updated file and reply with only its full contents."
        )
        result = session.run(
            "opencode",
            "--model",
            opencode_model,
            "run",
            "--format",
            "json",
            prompt,
            timeout=180,
        )
        assert (session.cwd / task.filename).read_text() == expected, launch
        session_id = opencode_completed_session(result.stdout)
        assert session_id not in sessions, "Repeat launch reused the previous session"
        sessions.add(session_id)
        exported = session.run("export", session_id, binary="opencode", timeout=30)
        assert_opencode_answer(
            json.loads(exported.stdout), session_id, opencode_model, expected, provider_id
        )

        config = json.loads(config_path.read_text())
        assert config["model"] == selector, launch
        provider = config["provider"][provider_id]
        assert provider["npm"] == "@ai-sdk/openai"
        assert provider["options"]["baseURL"] == f"{unmanaged_workspace}/ai-gateway/mlflow/v1"
        assert provider["models"][opencode_model]["provider"]["npm"] == expected_sdk
        assert set(provider["models"]) == set(discovered.get(bucket, []))
        for model in discovered.get(bucket, []):
            assert provider["models"][model] == configured["provider"][provider_id]["models"][model]
        assert {
            key: value
            for key, value in session.workspace_state().items()
            if key.endswith("_models")
            or key.endswith("_default_model")
            or key == "opencode_model_api_types"
        } == discovery_and_defaults, launch


def test_ug_opencode_rejects_missing_explicit_model(
    live_session, unmanaged_workspace, opencode_model
):
    """Scenario: configure OpenCode, then explicitly select a nonexistent model service.

    Expected: the real workspace returns not found, ug exits nonzero without
    falling back to a default model, and generated config and saved discovery stay unchanged.
    """
    session = live_session
    session.run(
        "configure",
        "--agents",
        "opencode",
        "--workspace",
        unmanaged_workspace,
        "--disable-databricks-ai-tools",
        timeout=240,
    )
    before = session.workspace_state()
    config_path = session.home / ".ucode/opencode-xdg/opencode/opencode.json"
    configured = config_path.read_bytes()
    missing = opencode_model.rsplit(".", 1)[0] + ".ug-integration-missing-" + uuid.uuid4().hex
    session.record("model.json", {"model": missing, "source": "nonexistent explicit model"})
    result = session.run(
        "opencode",
        "--model",
        missing,
        "run",
        "--format",
        "json",
        "Reply with ready.",
        ok=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "Could not resolve OpenCode model" in output and missing in output, output
    assert re.search(r"HTTP\s+404\b", output), output
    assert '"sessionID"' not in result.stdout, "An invalid selection reached the agent"
    assert config_path.read_bytes() == configured
    assert session.workspace_state() == before
