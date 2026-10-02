"""Claude transcript helpers for integration journeys."""

SESSION_DIRECTORY = ".claude/projects"


def is_child_session(path: str, records: list[dict]) -> bool:
    del records
    return "/subagents/" in path


def assistant_answers(records: list[dict]) -> list[str]:
    answers = []
    for record in records:
        if record.get("type") == "assistant":
            message = record.get("message", {})
            if message.get("role") == "assistant":
                answers.extend(
                    part["text"]
                    for part in message.get("content", [])
                    if part.get("type") == "text" and isinstance(part.get("text"), str)
                )
    return answers
