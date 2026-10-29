"""Grok Build-specific integration evidence: its `--output-format streaming-json` events, and the
chat history it saves for each session under ug's private Grok home."""

import json

from . import _evidence_id

__all__ = [
    "EVIDENCE_KIND",
    "answer",
    "assistant_answers",
    "completed_reads",
    "completed_mcp_calls",
    "completed_tool_calls",
    "completed_task_models",
    "end_event",
    "events",
    "turn_models",
]

# `<cwd, percent-encoded>/<session id>/chat_history.jsonl` under this directory.
SESSION_DIRECTORY = ".ucode/grok-home/sessions"
EVIDENCE_KIND = "assistant message model_id"


def events(stdout: str) -> list[dict]:
    """Grok's newline-delimited JSON events; ug's own status lines are not JSON and are skipped."""
    parsed = []
    for line in stdout.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            parsed.append(value)
    return parsed


def completed_tool_calls(records: list[dict]) -> list[tuple[str, dict]]:
    """``(tool name, input)`` of each tool call Grok reported as completed."""
    calls = {
        record.get("toolCallId"): (str(record.get("toolName")), record.get("rawInput") or {})
        for record in records
        if record.get("type") == "tool_call"
    }
    return [
        calls[record["toolCallId"]]
        for record in records
        if record.get("type") == "tool_call_update"
        and record.get("status") == "completed"
        and record.get("toolCallId") in calls
    ]


def completed_reads(records: list[dict]) -> list[dict]:
    """The inputs of `read_file` tool calls that Grok reported as completed."""
    return [raw for name, raw in completed_tool_calls(records) if name == "read_file"]


def completed_mcp_calls(records: list[dict], server: str) -> list[dict]:
    """Completed calls of ``server``'s MCP tools: by their `<server>__<tool>` name, or through
    Grok's `use_tool`. A `search_tool` lookup only finds tools and doesn't count."""
    return [
        raw
        for name, raw in completed_tool_calls(records)
        if name.startswith(f"{server}__") or (name == "use_tool" and server in json.dumps(raw))
    ]


def answer(records: list[dict]) -> str:
    """The assistant's streamed text; tool output arrives in tool events, never here."""
    return "".join(
        record["data"]
        for record in records
        if record.get("type") == "text" and isinstance(record.get("data"), str)
    )


def end_event(records: list[dict]) -> dict:
    ends = [record for record in records if record.get("type") == "end"]
    assert ends, "Grok emitted no end event"
    return ends[-1]


def turn_models(records: list[dict]) -> set[str]:
    """The model ids that served the turn, from the end event's per-model usage."""
    usage = end_event(records).get("modelUsage")
    assert isinstance(usage, dict) and usage, "Grok's end event reported no model usage"
    return {_evidence_id(model, "model") for model in usage}


def is_child_session(path: str, records: list[dict]) -> bool:
    del path, records
    return False


def _assistant_messages(records: list[dict]) -> list[dict]:
    """Assistant turns in a session's `chat_history.jsonl`; other session files have no such type."""
    return [record for record in records if record.get("type") == "assistant"]


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part["text"]
            for part in content
            if isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        )
    return ""


def assistant_answers(records: list[dict]) -> list[str]:
    return [
        text for message in _assistant_messages(records) if (text := _text(message.get("content")))
    ]


def completed_task_models(records: list[dict], answer_value: str) -> set[str]:
    """The model ids Grok recorded for the assistant messages that carry ``answer_value``."""
    return {
        _evidence_id(message.get("model_id"), "model")
        for message in _assistant_messages(records)
        if answer_value in _text(message.get("content"))
    }
