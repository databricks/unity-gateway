"""Copilot-specific integration evidence, read from Copilot's own session events."""

from . import _evidence_id

__all__ = [
    "EVIDENCE_KIND",
    "assistant_answers",
    "completed_task_models",
    "mcp_tool_calls",
    "selected_models",
]

SESSION_DIRECTORY = ".copilot/session-state"
EVIDENCE_KIND = "assistant message model"


def is_child_session(path: str, records: list[dict]) -> bool:
    del path, records
    return False


def _messages(records: list[dict]) -> list[dict]:
    return [
        record.get("data") or {} for record in records if record.get("type") == "assistant.message"
    ]


def assistant_answers(records: list[dict]) -> list[str]:
    return [
        message["content"]
        for message in _messages(records)
        if isinstance(message.get("content"), str)
    ]


def completed_task_models(records: list[dict], answer_value: str) -> set[str]:
    """The model ids Copilot sent for the assistant messages that carry ``answer_value``."""
    return {
        _evidence_id(message.get("model"), "model")
        for message in _messages(records)
        if answer_value in (message.get("content") or "")
    }


def selected_models(records: list[dict]) -> list[str]:
    """Registry refs the session started on or switched to, in order."""
    refs = []
    for record in records:
        data = record.get("data") or {}
        if record.get("type") == "session.start" and data.get("selectedModel"):
            refs.append(data["selectedModel"])
        elif record.get("type") == "session.model_change" and data.get("newModel"):
            refs.append(data["newModel"])
    return refs


def mcp_tool_calls(records: list[dict]) -> list[dict]:
    """Each MCP tool call: its server, the config source Copilot loaded it from, and whether
    the call completed successfully."""
    succeeded = {
        (record.get("data") or {}).get("toolCallId")
        for record in records
        if record.get("type") == "tool.execution_complete"
        and (record.get("data") or {}).get("success") is True
    }
    calls = []
    for record in records:
        data = record.get("data") or {}
        if record.get("type") == "tool.execution_start" and data.get("mcpServerName"):
            calls.append(
                {
                    "server": data["mcpServerName"],
                    "tool": data.get("mcpToolName"),
                    "source": data.get("mcpConfigSource"),
                    "succeeded": data.get("toolCallId") in succeeded,
                }
            )
    return calls
