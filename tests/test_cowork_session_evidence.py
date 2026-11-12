"""Offline checks prevent native Desktop task evidence from producing false passes."""

import json
import os
import runpy
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

session_evidence = runpy.run_path(
    str(Path(__file__).parents[1] / "scripts/cowork-inference/session-evidence.py")
)
evaluate = session_evidence["evaluate"]
inspect_sessions = session_evidence["inspect_sessions"]


@pytest.fixture
def evidence():
    task = {
        "capability": "skill",
        "prompt": "Use the fixture skill and read its reference file.",
        "expected": "proof-a12345",
        "expectedFile": r"C:\fixture\facts.txt",
        "skill": "fixture:summary",
    }
    records = [
        {"message": {"role": "user", "content": task["prompt"]}},
        {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "skill",
                        "name": "Skill",
                        "input": {"skill": task["skill"]},
                    },
                    {
                        "type": "tool_use",
                        "id": "read",
                        "name": "Read",
                        "input": {"file_path": task["expectedFile"]},
                    },
                ],
            }
        },
        {
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "skill", "content": "Launching skill"},
                    {
                        "type": "tool_result",
                        "tool_use_id": "read",
                        "content": "1→" + task["expected"],
                    },
                ],
            }
        },
        {
            "message": {
                "role": "assistant",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": task["expected"]}],
            }
        },
    ]
    return records, task


@pytest.mark.parametrize("capability", ["file", "skill"])
def test_requires_completed_native_file_and_skill_results(evidence, capability):
    records, task = evidence
    task["capability"] = capability
    if capability == "file":
        records[1]["message"]["content"].pop(0)
    assert evaluate(records, task)["status"] == "passed"


def test_accepts_windows_path_separators_used_by_native_read(evidence):
    records, task = evidence
    records[1]["message"]["content"][1]["input"]["file_path"] = r"c:\\fixture\\facts.txt"
    assert evaluate(records, task)["status"] == "passed"


@pytest.mark.parametrize("same_case", [True, False])
def test_macos_file_paths_preserve_case(evidence, same_case):
    records, task = evidence
    task.update(platform="macos", expectedFile="/Users/runner/CoworkProbe/input.txt")
    records[1]["message"]["content"][1]["input"]["file_path"] = (
        task["expectedFile"] if same_case else task["expectedFile"].lower()
    )
    assert evaluate(records, task)["status"] == ("passed" if same_case else "failed")


@pytest.mark.parametrize(
    "invalid",
    [
        "wrong_file",
        "wrong_contents",
        "read_error",
        "skill_error",
        "wrong_id",
        "missing_skill",
        "wrong_answer",
    ],
)
def test_rejects_invalid_tool_evidence(evidence, invalid):
    records, task = evidence
    calls = records[1]["message"]["content"]
    results = records[2]["message"]["content"]
    if invalid == "wrong_file":
        calls[1]["input"]["file_path"] = r"C:\other\facts.txt"
    elif invalid == "wrong_contents":
        results[1]["content"] = "different value"
    elif invalid == "read_error":
        results[1]["is_error"] = True
    elif invalid == "skill_error":
        results[0]["is_error"] = True
    elif invalid == "wrong_id":
        results[1]["tool_use_id"] = "unrelated"
    elif invalid == "missing_skill":
        calls.pop(0)
    elif invalid == "wrong_answer":
        records[3]["message"]["content"][0]["text"] = "different answer"
    assert evaluate(records, task)["status"] == "failed"


@pytest.mark.parametrize("tool_index", [0, 1], ids=["skill", "read"])
@pytest.mark.parametrize("value", [None, [], 42, "invalid"])
def test_malformed_tool_inputs_cannot_prove_execution(evidence, tool_index, value):
    records, task = evidence
    records[1]["message"]["content"][tool_index]["input"] = value
    result = evaluate(records, task)
    assert result["status"] == "failed"
    assert "No successful" in result["error"]


@pytest.mark.parametrize("value", [None, [], 42, {}])
def test_malformed_read_paths_cannot_prove_execution(evidence, value):
    records, task = evidence
    records[1]["message"]["content"][1]["input"]["file_path"] = value
    assert evaluate(records, task) == {
        "status": "failed",
        "error": "No successful Read of the expected file and contents",
    }


@pytest.mark.parametrize("record_index, field", [(1, "id"), (2, "tool_use_id")])
@pytest.mark.parametrize("value", [None, [], {}])
def test_malformed_tool_ids_cannot_prove_execution(evidence, record_index, field, value):
    records, task = evidence
    records[record_index]["message"]["content"][1][field] = value
    assert evaluate(records, task)["status"] == "failed"


@pytest.mark.parametrize("incomplete", ["other_prompt", "unfinished", "no_answer"])
def test_unrelated_or_incomplete_sessions_cannot_pass(evidence, incomplete):
    records, task = evidence
    if incomplete == "other_prompt":
        records[0]["message"]["content"] = "A different task"
    elif incomplete == "unfinished":
        records[3]["message"]["stop_reason"] = "tool_use"
    else:
        records.pop()
    assert evaluate(records, task)["status"] == "pending"


@pytest.mark.parametrize(
    "content", [None, [None, "unstructured text", {"type": "text", "text": None}]]
)
def test_ignores_unrelated_non_message_content(evidence, content):
    records, task = evidence
    records[:0] = [None, "metadata", {"message": {"role": "user", "content": content}}]
    records[4]["message"]["content"].extend([None, "metadata"])
    assert evaluate(records, task)["status"] == "passed"


@pytest.mark.parametrize("content", [None, ["proof-a12345"], [{"type": "text", "text": None}]])
def test_unstructured_tool_result_cannot_prove_a_read(evidence, content):
    records, task = evidence
    records[2]["message"]["content"][1]["content"] = content
    assert evaluate(records, task)["status"] == "failed"


@pytest.mark.parametrize("missing_at", ["stat", "read_text"])
@pytest.mark.parametrize("remaining_session", [False, True])
def test_transcript_rotation_preserves_pending_and_valid_sessions(
    evidence, tmp_path, monkeypatch, missing_at, remaining_session
):
    records, task = evidence
    task["startedAfter"] = datetime.fromtimestamp(0, tz=UTC).isoformat()
    directory = tmp_path / "projects" / "session"
    directory.mkdir(parents=True)
    disappearing = directory / "rotated.jsonl"
    disappearing.write_text("{}\n")
    if remaining_session:
        (directory / "active.jsonl").write_text("\n".join(map(json.dumps, records)))
    newest = time.time() + 60
    os.utime(disappearing, (newest, newest))
    original = getattr(Path, missing_at)

    def remove_during_inspection(path, *args, **kwargs):
        if path == disappearing:
            path.unlink(missing_ok=True)
        return original(path, *args, **kwargs)

    # Force the filesystem race at its boundary; the parser and surviving file stay real.
    monkeypatch.setattr(Path, missing_at, remove_during_inspection)
    result = inspect_sessions(tmp_path, task)
    assert result["status"] == ("passed" if remaining_session else "pending")


def test_transcript_permission_errors_are_not_hidden(tmp_path, monkeypatch):
    directory = tmp_path / "projects" / "session"
    directory.mkdir(parents=True)
    (directory / "unreadable.jsonl").write_text("{}\n")

    def denied(*args, **kwargs):
        raise PermissionError("unreadable transcript")

    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(PermissionError, match="unreadable transcript"):
        inspect_sessions(tmp_path, {"startedAfter": "1970-01-01T00:00:00+00:00"})


@pytest.mark.parametrize("template", ["1\t{}", "```\n1\t{}\n```"])
def test_tool_display_format_is_not_the_file_value(evidence, template):
    records, task = evidence
    records[-1]["message"]["content"][0]["text"] = template.format(task["expected"])
    assert evaluate(records, task)["status"] == "failed"


@pytest.fixture
def mcp_evidence():
    task = {
        "capability": "mcp",
        "prompt": "Use the web_search connector to find the Databricks website.",
        "mcpServer": "web_search",
        "mcpTool": "web_search",
    }
    records = [
        {"message": {"role": "user", "content": task["prompt"]}},
        {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "search",
                        "name": "mcp__web_search__web_search",
                        "input": {"query": "Databricks official website"},
                    }
                ],
            }
        },
        {
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "search",
                        "content": [
                            {"type": "text", "text": "Databricks (https://www.databricks.com/)"}
                        ],
                    }
                ],
            }
        },
        {
            "message": {
                "role": "assistant",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "**https://www.databricks.com**"}],
            }
        },
    ]
    return records, task


def test_mcp_search_requires_a_shared_official_databricks_url(mcp_evidence):
    records, task = mcp_evidence
    result = evaluate(records, task)
    assert result["status"] == "passed", result
    assert result["mcpToolName"] == "mcp__web_search__web_search"
    assert result["url"] == "https://www.databricks.com"


def _search_call(records):
    return records[1]["message"]["content"][0]


def _search_result(records):
    return records[2]["message"]["content"][0]


@pytest.mark.parametrize(
    "invalid",
    [
        "native_web_search",
        "other_tool",
        "empty_query",
        "tool_error",
        "missing_result",
        "lookalike_host",
        "answer_without_url",
        "answer_url_not_searched",
    ],
)
def test_mcp_search_rejects_unproven_or_unrelated_answers(mcp_evidence, invalid):
    records, task = mcp_evidence
    answer = records[3]["message"]["content"][0]
    match invalid:
        case "native_web_search":
            _search_call(records)["name"] = "WebSearch"
        case "other_tool":
            _search_call(records)["name"] = "mcp__web_search__fetch"
        case "empty_query":
            _search_call(records)["input"] = {"query": " "}
        case "tool_error":
            _search_result(records)["is_error"] = True
        case "missing_result":
            records.pop(2)
        case "lookalike_host":
            _search_result(records)["content"] = "https://databricks.com.example.org/"
            answer["text"] = "https://databricks.com.example.org"
        case "answer_without_url":
            answer["text"] = "The Databricks website."
        case "answer_url_not_searched":
            answer["text"] = "https://databricks.com/product"
    assert evaluate(records, task)["status"] == "failed"
