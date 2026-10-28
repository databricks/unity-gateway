"""Grok Build-specific integration evidence, read from its `--output-format streaming-json` events."""

import json

from . import _evidence_id

__all__ = ["answer", "completed_reads", "end_event", "events", "turn_models"]


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


def completed_reads(records: list[dict]) -> list[dict]:
    """The inputs of `read_file` tool calls that Grok reported as completed."""
    calls = {
        record.get("toolCallId"): record.get("rawInput") or {}
        for record in records
        if record.get("type") == "tool_call" and record.get("toolName") == "read_file"
    }
    return [
        calls[record["toolCallId"]]
        for record in records
        if record.get("type") == "tool_call_update"
        and record.get("status") == "completed"
        and record.get("toolCallId") in calls
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
