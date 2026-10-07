"""Real Copilot CLI journey through installed ug and the Databricks gateway."""

import json

import pytest
from utils.evidence import FileTask

pytestmark = [pytest.mark.live, pytest.mark.copilot]


@pytest.mark.smoke
def test_ug_copilot_claude_native_provider(live_session, workspace):
    """Scenario: configure Copilot and select a discovered Claude model at launch.

    Expected: ug writes the native Anthropic provider, and Copilot reads an unknown
    file value through the real gateway and returns it in a completed answer.
    """
    session = live_session
    task = FileTask(session)
    session.run(
        "configure",
        "--agents",
        "copilot",
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )
    model = session.workspace_state()["claude_models"]["sonnet"]
    session.record("copilot-model.json", {"model": model})

    result = session.run(
        "copilot",
        "--model",
        model,
        "-p",
        task.prompt,
        "--allow-all-tools",
        "--output-format",
        "json",
        "--no-auto-update",
        "--disable-builtin-mcps",
        timeout=180,
    )

    configured = dict(
        line.split("=", 1)
        for line in (session.home / ".copilot/ucode.env").read_text().splitlines()
    )
    assert configured["COPILOT_PROVIDER_TYPE"].strip('"') == "anthropic"
    assert configured["COPILOT_PROVIDER_BASE_URL"].strip('"') == (
        f"{workspace}/ai-gateway/anthropic"
    )
    events = []
    for line in result.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # ug writes its launch summary before the native JSON events.
    assert any(event.get("type") == "assistant.turn_end" for event in events)
    assert any(
        event.get("type") == "assistant.message"
        and task.value in (event.get("data") or {}).get("content", "")
        for event in events
    ), "Copilot did not return the file's unknown value in a completed answer."
