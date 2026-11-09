"""Offline checks prevent native Desktop task evidence from producing false passes."""

import runpy
from pathlib import Path

import pytest

evaluate = runpy.run_path(
    str(Path(__file__).parents[1] / "scripts/cowork-inference/session-evidence.py")
)["evaluate"]


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
