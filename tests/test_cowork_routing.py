"""Cowork session-start guidance for configured Unity Gateway integrations."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from ucode.agents.desktop import cowork, cowork_routing


def test_routing_guidance_names_configured_integrations():
    guidance = cowork_routing.routing_guidance(
        ["system-ai-github", "system-ai-atlassian", "custom-service", "system-ai-github"]
    )

    assert guidance.startswith(
        "Unity Gateway integrations available in this Cowork session: "
        "`unity-gateway:system-ai-github`, `unity-gateway:system-ai-atlassian`, "
        "`unity-gateway:custom-service`. "
    )
    assert "before saying the connector is unavailable" in guidance


@pytest.mark.parametrize("source", ["startup", "resume", "clear", "compact"])
def test_hook_output_injects_guidance_on_session_start(source):
    output = cowork_routing.hook_output(
        {"hook_event_name": "SessionStart", "source": source}, ["system-ai-github"]
    )

    hook_output = output["hookSpecificOutput"]
    assert hook_output["hookEventName"] == "SessionStart"
    assert "unity-gateway:system-ai-github" in hook_output["additionalContext"]


@pytest.mark.parametrize("payload", [{"hook_event_name": "UserPromptSubmit"}, {}, None])
def test_hook_output_ignores_other_events(payload):
    assert cowork_routing.hook_output(payload, ["system-ai-github"]) == {}


@pytest.mark.parametrize("event", ["SessionStart", "UserPromptSubmit"])
def test_generated_hook_command_emits_only_session_start_context(tmp_path, event):
    hook = cowork._routing_hooks(["system-ai-github"])["hooks"]["SessionStart"][0]["hooks"][0]
    result = subprocess.run(
        hook["command"],
        shell=True,
        input=json.dumps({"hook_event_name": event, "source": "startup"}),
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    expected = (
        {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": cowork_routing.routing_guidance(["system-ai-github"]),
            }
        }
        if event == "SessionStart"
        else {}
    )
    assert json.loads(result.stdout) == expected
    assert result.stderr == ""
