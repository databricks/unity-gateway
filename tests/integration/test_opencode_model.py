"""Explicit undiscovered-model journeys through installed ug and real OpenCode."""

import json
import re
import uuid

import pytest
from utils.evidence import FileTask, assert_opencode_answer, opencode_completed_session

pytestmark = [pytest.mark.live, pytest.mark.opencode]


def test_ug_opencode_explicit_undiscovered_model(live_session, unmanaged_workspace, opencode_model):
    """Scenario: configure OpenCode, then select an undiscovered model on two launches.

    Expected: each real headless run reads and edits a file, completes an assistant
    answer under the requested model, and retains the compatible SDK overlay.
    Curated discovery and saved defaults remain unchanged. This does not cover TUI use.
    """
    session = live_session
    session.run(
        "configure",
        "--agents",
        "opencode",
        "--workspace",
        unmanaged_workspace,
        "--disable-databricks-ai-tools",
    )
    before = session.workspace_state()
    curated = before["opencode_models"]
    assert curated, "ug configure found no curated OpenCode models"
    assert opencode_model not in {model for models in curated.values() for model in models}, (
        "--opencode-model must be outside ug's curated discovery to exercise this regression"
    )
    discovery_and_defaults = {
        key: value
        for key, value in before.items()
        if key.endswith("_models") or key.endswith("_default_model")
    }
    config_path = session.home / ".ucode/opencode-xdg/opencode/opencode.json"
    configured = json.loads(config_path.read_text())
    selector = f"databricks-oss/{opencode_model}"
    assert configured["model"] != selector
    session.record(
        "model.json",
        {
            "model": opencode_model,
            "selector": selector,
            "source": "runner override",
            "curated": curated,
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
        assert_opencode_answer(json.loads(exported.stdout), session_id, opencode_model, expected)

        config = json.loads(config_path.read_text())
        assert config["model"] == selector, launch
        provider = config["provider"]["databricks-oss"]
        assert provider["npm"] == "@ai-sdk/openai"
        assert provider["options"]["baseURL"] == f"{unmanaged_workspace}/ai-gateway/mlflow/v1"
        assert provider["models"][opencode_model]["provider"]["npm"] in {
            "@ai-sdk/openai",
            "@ai-sdk/openai-compatible",
        }
        assert set(provider["models"]) == set(curated.get("oss", [])) | {opencode_model}
        for model in curated.get("oss", []):
            assert (
                provider["models"][model]
                == configured["provider"]["databricks-oss"]["models"][model]
            )
        assert {
            key: value
            for key, value in session.workspace_state().items()
            if key.endswith("_models") or key.endswith("_default_model")
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
