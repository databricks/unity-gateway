"""User-authored skill input and native rollout evidence for the desktop journeys."""

from pathlib import Path

from .agents.codex import completed_task_models
from .evidence import agent_sessions

MCP_SERVICE = "system.ai.web_search"
MCP_SERVER = "system-ai-web_search"
SKILL_NAME = "ug-desktop-search"


def write_search_skill(home: Path, query: str, marker: str) -> Path:
    """An ordinary user skill, not generated ug state or a substitute MCP server."""
    skill = home / ".agents/skills" / SKILL_NAME
    reference = skill / "references/task.md"
    reference.parent.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {SKILL_NAME}\n"
        "description: Search with the configured Unity Gateway MCP service.\n---\n"
        "Read references/task.md beside this skill. It contains the search query and completion "
        f"token. Call the {MCP_SERVER} MCP server's web_search tool with that exact query. "
        "After the tool succeeds, reply with only the completion token. "
        "Do not use built-in web search or subagents.\n",
        encoding="utf-8",
    )
    reference.write_text(f"Query: {query}\nCompletion token: {marker}\n", encoding="utf-8")
    return skill


def assert_skill_mcp_turn(records: list[dict], query: str, marker: str, model: str) -> None:
    """Tie the hidden skill values and successful MCP receipts to the app's completed turn."""
    origins = [
        (row["payload"].get("originator"), row["payload"].get("model_provider"))
        for row in records
        if row.get("type") == "session_meta"
    ]
    assert origins == [("Codex Desktop", "Databricks")], origins
    assert completed_task_models(records, marker) == {model}
    turns = [
        row["payload"]["turn_id"]
        for row in records
        if row.get("type") == "event_msg"
        and row.get("payload", {}).get("type") == "task_complete"
        and row["payload"].get("last_agent_message", "").strip() == marker
    ]
    assert len(turns) == 1, f"Expected one completed desktop answer with the skill token: {turns}"
    calls = [
        item
        for row in records
        if row.get("type") == "event_msg"
        and (payload := row.get("payload", {})).get("type") == "item_completed"
        and payload.get("turn_id") == turns[0]
        and (item := payload.get("item", {})).get("type") == "McpToolCall"
        and item.get("server") == MCP_SERVER
        and item.get("tool") == "web_search"
    ]
    assert calls, "No native web_search receipt in the desktop's completed skill turn"
    for call in calls:
        assert call.get("arguments", {}).get("query") == query, call
        assert call.get("status") == "completed" and not call.get("error"), call
        result = call.get("result")
        assert isinstance(result, dict) and not result.get("isError"), call
        assert any(
            part.get("type") == "text"
            and isinstance(part.get("text"), str)
            and part["text"].strip()
            for part in result.get("content", [])
        ), call


def assert_desktop_skill_mcp_session(session, query: str, marker: str, model: str) -> None:
    sessions = agent_sessions(session, "codex")
    session.record("agent-sessions.json", sessions)
    assert len(sessions) == 1, f"Expected only the desktop session, got {list(sessions)}"
    assert_skill_mcp_turn(next(iter(sessions.values())), query, marker, model)
