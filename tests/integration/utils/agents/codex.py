"""Codex transcript helpers for integration journeys."""

SESSION_DIRECTORY = ".codex/sessions"


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
