"""Codex-specific integration evidence and model discovery helpers."""

import re
import shlex
import sqlite3
from contextlib import closing
from pathlib import Path

from ..model_discovery import codex_model_in_picker as model_in_picker
from . import _evidence_id

__all__ = [
    "EVIDENCE_KIND",
    "assert_agent_interaction_completed",
    "assistant_answers",
    "completed_task_models",
    "completed_command_outputs",
    "model_in_picker",
]

SESSION_DIRECTORY = ".codex/sessions"
EVIDENCE_KIND = "client-selected model"
DIAGNOSTIC_TARGET = "codex_otel.trace_safe"
_NATIVE_LOG_HEADER = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}T[\d:.]+(?:Z|[+-]\d{2}:\d{2}))"
    r"\s+(?:TRACE|DEBUG|INFO|WARN|ERROR)\s+"
)
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_DIAGNOSTIC_FIELDS = frozenset(
    {
        "event.name",
        "event.timestamp",
        "conversation.id",
        "duration_ms",
        "attempt",
        "http.response.status_code",
        "auth.request_id",
        "request_id",
        "status",
        "status_code",
        "event.kind",
    }
)


def diagnostic_events(text: str) -> list[dict[str, str]]:
    events = []
    header = _NATIVE_LOG_HEADER.match(text)
    fields = {"event.timestamp": header["timestamp"]} if header else {}
    lexer = shlex.shlex(text, posix=True, punctuation_chars="\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        for token in lexer:
            if not token.strip("\n"):
                if fields:
                    events.append(fields)
                fields = {}
                continue
            name, _, value = token.partition("=")
            if name == "error.message":
                if fields:
                    events.append(fields)
                return events
            pattern = (
                r"[a-zA-Z0-9_.:/+-]{1,128}"
                if name == "event.timestamp"
                else r"[a-zA-Z0-9_.:/-]{1,128}"
            )
            if name in _DIAGNOSTIC_FIELDS and re.fullmatch(pattern, value):
                fields[name] = value
    except ValueError:
        return events
    if fields:
        events.append(fields)
    return events


def split_diagnostic_output(text: str) -> tuple[str, list[dict[str, str]]]:
    ordinary = []
    events = []
    record = []
    capture = False
    for line in text.splitlines(keepends=True):
        clean = _ANSI_ESCAPE.sub("", line)
        header = _NATIVE_LOG_HEADER.match(clean)
        native = bool(header and f" {DIAGNOSTIC_TARGET}:" in clean)
        if header:
            if record:
                events.extend(diagnostic_events("".join(record)))
                record = []
            capture = native
        if capture:
            record.append(clean)
        else:
            ordinary.append(line)
    if record:
        events.extend(diagnostic_events("".join(record)))
    return "".join(ordinary), events


def native_diagnostics(home: Path) -> dict:
    databases = {}
    for path in (home / ".codex").glob("logs_*.sqlite"):
        try:
            with closing(
                sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
            ) as connection:
                rows = connection.execute(
                    "SELECT ts, level, target, thread_id, feedback_log_body FROM logs "
                    "WHERE target LIKE 'codex_api%' OR target LIKE 'codex_http_client%' "
                    "OR target LIKE 'codex_core::client%' ORDER BY id DESC LIMIT 2000"
                ).fetchall()
            databases[path.name] = {
                "latest_rows_limit": 2000,
                "events": [
                    {
                        "timestamp": ts,
                        "level": level,
                        "target": target,
                        "thread_id": thread_id,
                        "fields": diagnostic_events(body or ""),
                    }
                    for ts, level, target, thread_id, body in reversed(rows)
                ],
            }
        except sqlite3.DatabaseError as error:
            databases[path.name] = {"read_error": type(error).__name__}
    return databases


def assert_agent_interaction_completed(records: list[dict], target: str, thread_id: str) -> None:
    """Require the native delivery receipt, shared by direct tools and code mode."""
    assert any(
        row.get("type") == "event_msg"
        and row.get("payload", {}).get("type") == "item_completed"
        and (item := row["payload"].get("item", {})).get("type") == "SubAgentActivity"
        and item.get("kind") == "interacted"
        and item.get("agent_path") == target
        and item.get("agent_thread_id") == thread_id
        for row in records
    ), f"No successful native interaction with {target} ({thread_id})"


def _command_text(command: object) -> str:
    if isinstance(command, str):
        return command
    if isinstance(command, list) and all(isinstance(part, str) for part in command):
        return " ".join(command)
    return ""


def completed_command_outputs(
    records: list[dict], *, command_contains: str | None = None
) -> list[str]:
    return [
        item["aggregated_output"]
        for row in records
        if row.get("type") == "event_msg"
        and row.get("payload", {}).get("type") == "item_completed"
        and (item := row["payload"].get("item", {})).get("type") == "CommandExecution"
        and item.get("status") == "completed"
        and item.get("exit_code") == 0
        and isinstance(item.get("aggregated_output"), str)
        and (command_contains is None or command_contains in _command_text(item.get("command")))
    ]


def is_child_session(path: str, records: list[dict]) -> bool:
    del path
    return any(
        record.get("type") == "session_meta"
        and isinstance(record.get("payload", {}).get("source"), dict)
        and "subagent" in record["payload"]["source"]
        for record in records
    )


def assistant_answers(records: list[dict]) -> list[str]:
    answers = []
    for record in records:
        if record.get("type") == "event_msg":
            payload = record.get("payload", {})
            if payload.get("type") == "task_complete" and payload.get("last_agent_message"):
                answers.append(payload["last_agent_message"])
    return answers


def completed_task_models(records: list[dict], answer_value: str) -> set[str]:
    """Join Codex's completed answer turn to its client-selected context model."""
    assert isinstance(answer_value, str) and answer_value.strip(), (
        "Expected a nonempty answer value"
    )
    completed: set[str] = set()
    for record in records:
        if record.get("type") != "event_msg":
            continue
        payload = record.get("payload")
        assert isinstance(payload, dict), "Malformed Codex event payload"
        if payload.get("type") != "task_complete":
            continue
        answer = payload.get("last_agent_message")
        assert isinstance(answer, str), "Missing or malformed Codex completed answer"
        if answer_value in answer:
            completed.add(_evidence_id(payload.get("turn_id"), "turn ID"))
    models: dict[str, set[str]] = {turn_id: set() for turn_id in completed}
    for record in records:
        if record.get("type") != "turn_context":
            continue
        payload = record.get("payload")
        assert isinstance(payload, dict), "Malformed Codex turn context"
        turn_id = _evidence_id(payload.get("turn_id"), "turn ID")
        if turn_id in completed:
            models[turn_id].add(_evidence_id(payload.get("model"), "model"))
    assert all(models.values()), "Missing Codex model context for completed answer turn"
    return {model for turn_models in models.values() for model in turn_models}
