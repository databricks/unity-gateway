"""Claude-specific integration evidence and model discovery helpers."""

import json
from pathlib import Path
from typing import Literal

from ..model_discovery import claude_discovery_model_id as discovery_model_id
from ..model_discovery import claude_model_in_picker as model_in_picker
from . import _evidence_id

__all__ = [
    "EVIDENCE_KIND",
    "assistant_answers",
    "assert_follow_ups_resumed",
    "child_id",
    "claude_delivered_child_answers",
    "classify_send_message",
    "completed_task_models",
    "discovery_model_id",
    "model_in_picker",
]

SESSION_DIRECTORY = ".claude/projects"
EVIDENCE_KIND = "response-reported model"


def is_child_session(path: str, records: list[dict]) -> bool:
    del records
    return "/subagents/" in path


def assistant_answers(records: list[dict]) -> list[str]:
    answers = []
    for record in records:
        message = record.get("message", {})
        if record.get("type") == "assistant" and message.get("role") == "assistant":
            for part in message.get("content", []):
                if part.get("type") == "text" and isinstance(part.get("text"), str):
                    answers.append(part["text"])
                elif _is_subagent_handback(record, part):
                    answers.append(part["input"]["message"])
    return answers


def _is_subagent_handback(record: dict, part: dict) -> bool:
    return (
        record.get("isSidechain") is True
        and part.get("type") == "tool_use"
        and part.get("name") == "SubagentHandback"
        and isinstance(part.get("id"), str)
        and isinstance(part.get("input", {}).get("message"), str)
    )


SendOutcome = Literal["resumed", "queued", "rejected"]


def _is_queued_to(result: object, agent_id: str) -> bool:
    """True for a successful SendMessage result queued to the still-running `agent_id`."""
    return (
        isinstance(result, dict)
        and result.get("success") is True
        and "resumedAgentId" not in result
        and "queued" in str(result.get("message", "")).lower()
        and isinstance(result.get("pin"), dict)
        and result["pin"].get("id") == agent_id
    )


def _is_rejected_for(call: dict, result: object, agent_id: str) -> bool:
    """True for a failed (undelivered) SendMessage addressed to `agent_id`.

    Claude's rejection (for example an unsupported `notify_when_idle`) carries only
    `success: False` and a message, so the child is identified by the call's `to` or,
    when present, the result's `pin.id`.
    """
    if not isinstance(result, dict) or result.get("success") is not False:
        return False
    if "resumedAgentId" in result:
        return False
    pin = result.get("pin")
    return call["input"]["to"] == agent_id or (isinstance(pin, dict) and pin.get("id") == agent_id)


def _native_result(call: dict, records: list[dict]) -> object:
    """Return the native tool result recorded for one parent SendMessage call.

    Raises AssertionError for a missing or empty recipient or a missing tool result.
    """
    recipient = call.get("input", {}).get("to")
    assert isinstance(recipient, str) and recipient.strip(), (
        f"SendMessage requires a nonempty 'to' recipient: {call}"
    )
    for record in records:
        content = record.get("message", {}).get("content")
        if record.get("type") != "user" or not isinstance(content, list):
            continue
        if any(part.get("tool_use_id") == call["id"] for part in content):
            return record.get("toolUseResult")
    raise AssertionError(f"No native tool result for SendMessage {call['id']}: {call}")


def classify_send_message(call: dict, records: list[dict], agent_id: str) -> SendOutcome:
    """Classify one parent SendMessage for the original child `agent_id`.

    Returns "resumed" (the native result resolved to the child), "queued" (accepted for
    the still-running child) or "rejected" (addressed to the child but not delivered).
    Raises AssertionError for a missing recipient or result, a result for another agent,
    or any other shape.
    """
    recipient = call.get("input", {}).get("to")
    result = _native_result(call, records)
    # The native result resolves both names and IDs to the resumed child's ID.
    if isinstance(result, dict) and result.get("resumedAgentId") == agent_id:
        return "resumed"
    if _is_queued_to(result, agent_id):
        return "queued"
    if _is_rejected_for(call, result, agent_id):
        return "rejected"
    raise AssertionError(
        f"SendMessage to {recipient!r} neither resumed nor was queued or rejected "
        f"for {agent_id}: {result}"
    )


def assert_follow_ups_resumed(calls: list[dict], records: list[dict]) -> str:
    """Resolve the single child the parent's follow-up SendMessage calls resumed.

    Requires exactly one distinct, nonempty `resumedAgentId` across the native results,
    every send to target that child, and at least one real resume, so the journey can
    assert against the child the follow-up actually continued even when redundant
    duplicate spawns exist. A redundant follow-up the model sends while the child runs
    may be queued or rejected; that is tolerated only alongside a real resume.
    """
    assert calls, "Expected at least one SendMessage"
    resumed = {
        result["resumedAgentId"]
        for call in calls
        for result in [_native_result(call, records)]
        if isinstance(result, dict) and result.get("resumedAgentId")
    }
    assert len(resumed) == 1, (
        f"Expected the follow-ups to resume exactly one child, got {sorted(resumed)}"
    )
    agent_id = resumed.pop()
    outcomes = [classify_send_message(call, records, agent_id) for call in calls]
    assert "resumed" in outcomes, f"No SendMessage resumed {agent_id}: {outcomes}"
    return agent_id


def completed_task_models(records: list[dict], answer_value: str) -> set[str]:
    """Parse Claude's response-reported model on the assistant's matching answer."""
    assert isinstance(answer_value, str) and answer_value.strip(), (
        "Expected a nonempty answer value"
    )
    found: set[str] = set()
    for record in records:
        if record.get("type") != "assistant":
            continue
        message = record.get("message")
        assert isinstance(message, dict), "Malformed Claude assistant message"
        assert message.get("role") == "assistant", "Missing Claude assistant role"
        content = message.get("content")
        assert isinstance(content, list), "Malformed Claude assistant content"
        for part in content:
            assert isinstance(part, dict), "Malformed Claude assistant content block"
            if part.get("type") != "text":
                continue
            text = part.get("text")
            assert isinstance(text, str), "Malformed Claude assistant text"
            if answer_value in text:
                model = _evidence_id(message.get("model"), "model")
                found.add(_evidence_id(model.removesuffix("[1m]"), "model"))
    return found


def child_id(path: str) -> str:
    """The child ID encoded in a Claude child transcript path's file name."""
    return Path(path).stem.removeprefix("agent-")


SPAWN_TOOLS = {"Agent", "Task"}


def parent_tool_calls(sessions: dict[str, list[dict]], names: set[str]) -> dict[str, dict]:
    """Parent tool_use calls with one of ``names``, keyed by call ID."""
    return {
        part["id"]: part
        for path, records in sessions.items()
        if not is_child_session(path, records)
        for record in records
        if record.get("type") == "assistant"
        for part in record.get("message", {}).get("content", [])
        if part.get("type") == "tool_use" and part.get("name") in names
    }


def child_paths(sessions: dict[str, list[dict]]) -> set[str]:
    return {path for path, records in sessions.items() if is_child_session(path, records)}


def assert_one_delegated_task(sessions: dict[str, list[dict]]) -> None:
    """At least one child, and every parent spawn is a byte-identical copy of one task.

    Claude may re-spawn the identical task while it waits for the first child's
    completion notification (run 42721826953); a differently tasked spawn still fails.
    """
    assert child_paths(sessions), "the parent spawned no child session"
    spawns = parent_tool_calls(sessions, SPAWN_TOOLS)
    assert len({json.dumps(call["input"], sort_keys=True) for call in spawns.values()}) == 1, (
        sorted(spawns.values(), key=lambda call: call["id"])
    )


def assert_follow_up_spawned_nothing(
    before: dict[str, list[dict]], after: dict[str, list[dict]]
) -> None:
    """The follow-up phase created no child session and made no spawn call."""
    assert child_paths(after) == child_paths(before), (
        sorted(child_paths(before)),
        sorted(child_paths(after)),
    )
    spawns_before = set(parent_tool_calls(before, SPAWN_TOOLS))
    spawns_after = set(parent_tool_calls(after, SPAWN_TOOLS))
    assert spawns_after == spawns_before, sorted(spawns_after ^ spawns_before)


def claude_delivered_child_answers(sessions: dict[str, list[dict]], value: str) -> set[str]:
    """Find children that answered and whose answer reached their own parent transcript.

    A peer delivery must be a native user-role record with the child's sender ID,
    not a prompt, tool input, or assistant-generated notification. A parent
    assistant answer also counts, but only alongside that child's own answer.
    """
    matched: set[str] = set()
    for path, records in sessions.items():
        if not is_child_session(path, records):
            continue
        sender_id = child_id(path)
        child_path = Path(path)
        parent_path = child_path.parent.parent.as_posix() + ".jsonl"
        if not any(value in answer for answer in assistant_answers(records)):
            continue
        parent_answer = False
        delivered = False
        for record in sessions.get(parent_path, []):
            if any(value in answer for answer in assistant_answers([record])):
                parent_answer = True
            origin = record.get("origin", {})
            if (
                record.get("type") == "user"
                and record.get("isMeta") is True
                and isinstance(record.get("message", {}).get("content"), str)
                and value in record["message"]["content"]
                and origin.get("kind") == "peer"
                and origin.get("from") == sender_id
            ):
                delivered = True
        if parent_answer or delivered:
            matched.add(sender_id)
    return matched
