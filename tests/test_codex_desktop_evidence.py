"""Reject false positives in the native desktop skill/MCP evidence checks."""

import copy

import pytest

from tests.integration.utils.codex_desktop import (
    MCP_SERVER,
    SKILL_NAME,
    assert_skill_mcp_turn,
    write_search_skill,
)

QUERY = "Databricks Unity Catalog documentation a13b42"
MARKER = "b39c21"
MODEL = "system.ai.gpt-5-6-sol"


@pytest.fixture
def records():
    return [
        {
            "type": "session_meta",
            "payload": {"originator": "Codex Desktop", "model_provider": "Databricks"},
        },
        {"type": "turn_context", "payload": {"turn_id": "turn-1", "model": MODEL}},
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": "turn-1",
                "item": {
                    "type": "McpToolCall",
                    "id": "call-1",
                    "server": MCP_SERVER,
                    "tool": "web_search",
                    "arguments": {"query": QUERY},
                    "status": "completed",
                    "result": {"content": [{"type": "text", "text": "Databricks documentation"}]},
                },
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "turn-1",
                "last_agent_message": MARKER,
            },
        },
    ]


def test_skill_values_are_only_in_the_reference_file(tmp_path):
    skill = write_search_skill(tmp_path, QUERY, MARKER)

    assert skill == tmp_path / ".agents/skills" / SKILL_NAME
    instructions = (skill / "SKILL.md").read_text()
    assert "references/task.md" in instructions and MCP_SERVER in instructions
    assert QUERY not in instructions and MARKER not in instructions
    assert (skill / "references/task.md").read_text() == (
        f"Query: {QUERY}\nCompletion token: {MARKER}\n"
    )


def test_native_mcp_receipt_proves_execution_including_code_mode(records):
    # Native receipts establish execution even when functions.exec hides the direct model call.
    assert_skill_mcp_turn(records, QUERY, MARKER, MODEL)
    extra_call = copy.deepcopy(records[2])
    extra_call["payload"]["item"]["id"] = "call-2"
    records.insert(3, extra_call)
    assert_skill_mcp_turn(records, QUERY, MARKER, MODEL)


@pytest.mark.parametrize(
    ("index", "changes"),
    [
        (0, {"originator": "codex_cli_rs"}),
        (0, {"model_provider": "OpenAI"}),
        (1, {"model": "another-model"}),
        (1, {"turn_id": "another-turn"}),
        (2, {"turn_id": "earlier-turn"}),
        (2, {"type": "item_started"}),
        (3, {"type": "agent_message"}),
        (3, {"last_agent_message": f"I could not finish {MARKER}"}),
    ],
)
def test_wrong_origin_model_turn_or_answer_fails(records, index, changes):
    records[index]["payload"].update(changes)
    with pytest.raises(AssertionError):
        assert_skill_mcp_turn(records, QUERY, MARKER, MODEL)


@pytest.mark.parametrize(
    "changes",
    [
        {"type": "WebSearch"},
        {"server": "another-server"},
        {"tool": "another-tool"},
        {"arguments": {"query": "an unrelated query"}},
        {"status": "inProgress"},
        {"status": "failed"},
        {"error": {"message": "permission denied"}},
        {"result": None},
        {"result": {"isError": True, "content": [{"type": "text", "text": "error"}]}},
        {"result": {"content": []}},
        {"result": {"content": [{"type": "text", "text": " "}]}},
    ],
)
def test_every_matching_mcp_receipt_must_succeed(records, changes):
    records[2]["payload"]["item"].update(changes)
    with pytest.raises(AssertionError):
        assert_skill_mcp_turn(records, QUERY, MARKER, MODEL)


def test_success_cannot_hide_another_failed_matching_call(records):
    failed_call = copy.deepcopy(records[2])
    failed_call["payload"]["item"].update(id="failed-call", status="failed")
    records.insert(3, failed_call)
    with pytest.raises(AssertionError):
        assert_skill_mcp_turn(records, QUERY, MARKER, MODEL)


def test_final_answer_alone_cannot_prove_tool_use(records):
    del records[2]
    with pytest.raises(AssertionError, match="No native web_search receipt"):
        assert_skill_mcp_turn(records, QUERY, MARKER, MODEL)
