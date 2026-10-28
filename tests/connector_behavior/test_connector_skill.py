"""Real Claude decisions with generated guidance and synthetic connector tools.

Synthetic Slack results isolate skill compliance from Slack credentials and client
reconnection. They do not establish native Cowork discovery or resume behavior.
"""

import json
import secrets
import time

import pytest

from ucode.agents.desktop.cowork import CONNECTOR_SKILL_NAME, _write_connector_skill

SLACK_TOOL_NAME = "mcp__plugin_ug_slack__search"
SLACK_TOOL = {
    "name": SLACK_TOOL_NAME,
    "description": "Search Slack messages by channel and query.",
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    },
}
DISCOVERY_TOOL = {
    "name": "ToolSearch",
    "description": "Discover currently callable connector tools by service or task.",
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    },
}
WAIT_TOOL = {
    "name": "wait",
    "description": "Wait for the specified number of seconds before continuing.",
    "input_schema": {
        "type": "object",
        "properties": {"seconds": {"type": "integer"}},
        "required": ["seconds"],
        "additionalProperties": False,
    },
}


def _run_resumed_request(tmp_path, claude_messages, *, availability, skill_loaded):
    _write_connector_skill(tmp_path, ["slack"])
    text = (tmp_path / CONNECTOR_SKILL_NAME / "SKILL.md").read_text(encoding="utf-8")
    description = json.loads(text.splitlines()[2].removeprefix("description: "))
    body = text.split("---\n", 2)[2]
    skill_tool = {
        "name": "Skill",
        "description": "Load an available skill's full instructions.",
        "input_schema": {
            "type": "object",
            "properties": {"skill": {"type": "string"}},
            "required": ["skill"],
            "additionalProperties": False,
        },
    }
    system = (
        "You are Claude, helping the user with their work. Use tools to obtain external data.\n"
        f"Available skills:\n{CONNECTOR_SKILL_NAME}: {description}"
    )
    conversation = []
    if skill_loaded:
        conversation.extend(
            [
                {"role": "user", "content": "Please check coding-pod for desktop app docs."},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "previous_skill",
                            "name": "Skill",
                            "input": {"skill": CONNECTOR_SKILL_NAME},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "previous_skill", "content": body}
                    ],
                },
                {"role": "assistant", "content": "Ready to check coding-pod."},
            ]
        )
    conversation.append(
        {
            "role": "user",
            "content": (
                "<system-reminder>The session resumed after an idle break. "
                "MCP servers disconnected: slack.</system-reminder>\n"
                "Can you check in coding-pod channel? What do I need to do for desktop app docs?"
            ),
        }
    )
    marker = "DOCS-" + secrets.token_hex(3)
    calls = []
    waits = 0
    for _ in range(16):
        ready = availability == "available" or (availability == "pending" and waits > 0)
        tools = [skill_tool, DISCOVERY_TOOL, WAIT_TOOL]
        if ready:
            tools.append(SLACK_TOOL)
        response = claude_messages(system, conversation, tools)
        content = response["content"]
        conversation.append({"role": "assistant", "content": content})
        actions = [block for block in content if block["type"] == "tool_use"]
        if not actions:
            assert response["stop_reason"] == "end_turn", response
            answer = "\n".join(block["text"] for block in content if block["type"] == "text")
            return calls, answer, marker
        assert response["stop_reason"] == "tool_use", response
        results = []
        for action in actions:
            name, arguments = action["name"], action["input"]
            calls.append((name, arguments))
            if name == "Skill":
                assert arguments == {"skill": CONNECTOR_SKILL_NAME}
                result = body
            elif name == "ToolSearch":
                result = (
                    json.dumps([SLACK_TOOL]) if ready else "No matching tools. Status: pending."
                )
            elif name == "wait":
                assert not ready, "Claude waited even though the Slack tool was exposed"
                assert arguments == {"seconds": 5}
                waits += 1
                assert waits <= 6, "Claude exceeded the skill's bounded wait"
                time.sleep(5)
                result = "Waited 5 seconds."
            elif name == SLACK_TOOL_NAME:
                assert ready, "Claude guessed a tool that was not exposed"
                assert "coding-pod" in arguments["query"]
                result = (
                    f"coding-pod: Update the Desktop setup guide with documentation task {marker}."
                )
            else:
                pytest.fail(f"Claude called an unexposed tool: {name}")
            results.append({"type": "tool_result", "tool_use_id": action["id"], "content": result})
        conversation.append({"role": "user", "content": results})
    pytest.fail(f"Claude did not finish within 16 model turns; calls: {calls}")


@pytest.mark.parametrize(
    "skill_loaded", [False, True], ids=["description-only", "loaded-before-break"]
)
def test_calls_available_slack_despite_stale_disconnect_notice(
    tmp_path, claude_messages, skill_loaded
):
    """Scenario: a resumed request has a disconnect notice but a callable Slack tool.

    Expected: Claude calls Slack and answers using its result in the same turn.
    """
    calls, answer, marker = _run_resumed_request(
        tmp_path, claude_messages, availability="available", skill_loaded=skill_loaded
    )
    assert any(name == SLACK_TOOL_NAME for name, _ in calls), answer
    assert marker in answer, answer
    assert not any(name == "wait" for name, _ in calls), calls


@pytest.mark.parametrize(
    "skill_loaded", [False, True], ids=["description-only", "loaded-before-break"]
)
def test_waits_then_calls_slack_when_tools_appear_after_resume(
    tmp_path, claude_messages, skill_loaded
):
    """Scenario: Slack discovery is pending until an actual five-second wait finishes.

    Expected: Claude discovers, waits once, calls the newly exposed tool, and answers.
    """
    calls, answer, marker = _run_resumed_request(
        tmp_path, claude_messages, availability="pending", skill_loaded=skill_loaded
    )
    names = [name for name, _ in calls]
    assert "ToolSearch" in names, answer
    assert names.count("wait") == 1, (calls, answer)
    assert SLACK_TOOL_NAME in names, answer
    assert names.index("wait") < names.index(SLACK_TOOL_NAME), calls
    assert marker in answer, answer


def test_reports_missing_tools_only_after_bounded_wait(tmp_path, claude_messages):
    """Scenario: a resumed session never exposes Slack tools.

    Expected: Claude performs six waits before reporting missing tools without invented auth.
    """
    calls, answer, _ = _run_resumed_request(
        tmp_path, claude_messages, availability="absent", skill_loaded=True
    )
    names = [name for name, _ in calls]
    assert names.count("wait") == 6, (calls, answer)
    assert "ToolSearch" in names, answer
    assert SLACK_TOOL_NAME not in names, calls
    assert "tools" in answer.lower(), answer
    assert not any(
        claim in answer.lower()
        for claim in ("has disconnected", "need to reconnect", "token expired", "need to sign in")
    ), answer
