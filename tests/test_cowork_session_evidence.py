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


def test_old_tool_calls_cannot_satisfy_a_new_prompt(evidence):
    records, task = evidence
    records.insert(2, records.pop(0))
    assert evaluate(records, task)["status"] == "failed"


def test_repeated_skill_invocation_fails_duplicate_check(evidence):
    records, task = evidence
    records[1]["message"]["content"].append(
        {"type": "tool_use", "id": "another", "name": "Skill", "input": {"skill": task["skill"]}}
    )
    records[2]["message"]["content"].append(
        {"type": "tool_result", "tool_use_id": "another", "content": "Launching skill"}
    )
    assert evaluate(records, task)["status"] == "failed"


@pytest.mark.parametrize(
    "names,passed",
    [
        (["ug-native-contract:probe-summary"], True),
        (["probe-summary", "ug-native-contract:probe-summary"], False),
        (["ug-native-contract:probe-summary", "ug-native-contract:probe-summary"], False),
        ([], False),
    ],
)
def test_native_inventory_requires_one_intended_source(evidence, names, passed):
    records, task = evidence
    task.update(requireInventory=True, skill="ug-native-contract:probe-summary")
    records[1]["message"]["content"][0]["input"]["skill"] = task["skill"]
    records.append(
        {
            "attachment": {
                "type": "skill_listing",
                "content": "\n".join(f"- {name}: description" for name in names),
            }
        }
    )
    assert evaluate(records, task)["status"] == ("passed" if passed else "failed")


def test_unrelated_session_inventory_cannot_fail_the_task(evidence):
    records, task = evidence
    task["requireInventory"] = True
    records[0]["message"]["content"] = "unrelated prompt"
    records.append({"attachment": {"type": "skill_listing", "content": ""}})
    assert evaluate(records, task)["status"] == "pending"


@pytest.mark.parametrize("allow_cache", [False, True])
def test_only_org_plugin_can_use_native_cache_path(evidence, allow_cache):
    records, task = evidence
    task["allowPluginCache"] = allow_cache
    records[1]["message"]["content"][1]["input"]["file_path"] = (
        r"C:\Users\consumer\cache\ug-native-contract\skills\probe-summary\references\facts.txt"
    )
    assert evaluate(records, task)["status"] == ("passed" if allow_cache else "failed")


@pytest.mark.parametrize("fault", [None, "missing", "duplicate", "other_session"])
def test_org_plugin_hooks_must_execute_once_for_this_session(evidence, tmp_path, fault):
    records, task = evidence
    task.update(requireHooks=True, eventsDirectory=str(tmp_path))
    for record in records:
        record.update(sessionId="active", version="2.1.290")
    events = [
        {"session_id": "active", "hook_event_name": "SessionStart"},
        {"session_id": "active", "hook_event_name": "UserPromptSubmit"},
    ]
    if fault == "missing":
        events.pop()
    elif fault == "duplicate":
        events.append(events[0])
    elif fault == "other_session":
        events[0]["session_id"] = "unrelated"
    for index, event in enumerate(events):
        (tmp_path / f"{index}.json").write_text(json.dumps(event))
    result = evaluate(records, task)
    assert result["status"] == ("failed" if fault else "passed")
    if not fault:
        assert result["engineVersions"] == ["2.1.290"]


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


@pytest.mark.parametrize("matches_prompt", [False, True])
def test_pending_transcript_retained_only_for_matching_task(evidence, tmp_path, matches_prompt):
    records, task = evidence
    task["startedAfter"] = datetime.fromtimestamp(0, tz=UTC).isoformat()
    if not matches_prompt:
        records[0]["message"]["content"] = "An unrelated task"
    transcript = tmp_path / "pending.jsonl"
    transcript.write_text(json.dumps(records[0]))

    result = inspect_sessions(tmp_path, task)

    assert result["status"] == "pending"
    assert result.get("transcript") == (str(transcript) if matches_prompt else None)


def test_transcript_permission_errors_are_not_hidden(tmp_path, monkeypatch):
    directory = tmp_path / "projects" / "session"
    directory.mkdir(parents=True)
    (directory / "unreadable.jsonl").write_text("{}\n")

    def denied(*args, **kwargs):
        raise PermissionError("unreadable transcript")

    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(PermissionError, match="unreadable transcript"):
        inspect_sessions(tmp_path, {"startedAfter": "1970-01-01T00:00:00+00:00"})
