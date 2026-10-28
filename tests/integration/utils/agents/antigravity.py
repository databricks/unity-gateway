"""Antigravity CLI evidence, read from its own stream-json output, conversation store, and log."""

import json
import re
import sqlite3
from pathlib import Path

STATE_DIRECTORY = ".gemini/antigravity-cli"
# `steps.step_type` of an agent response in Antigravity's conversation store (observed on 1.4.9;
# 14 is the user's input and tool calls have their own types, so a tool result never counts).
AGENT_RESPONSE_STEP = 15
DONE_STATUS = 3
# Antigravity logs every model request's URL; in gateway mode it names the gateway it was given.
_REQUEST = re.compile(r"Making Gateway streaming request to: (\S+)")


def stream_events(stdout: str) -> list[dict]:
    """The JSON events of `agy -p --output-format stream-json`; ug's launch status is skipped."""
    events = []
    for line in stdout.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "event" in value:
            events.append(value)
    return events


def completed_tools(events: list[dict], name: str) -> list[dict]:
    """``tool_info`` of every finished call to tool ``name``."""
    return [
        update.get("tool_info") or {}
        for event in events
        if event.get("event") == "step_update"
        and (update := event.get("step_update") or {}).get("step_type") == "tool"
        and update.get("tool_name") == name
        and update.get("state") == "DONE"
    ]


def final_response(events: list[dict]) -> str:
    """The successful run's final answer (the `result` event, never echoed input)."""
    results = [event.get("result") or {} for event in events if event.get("event") == "result"]
    assert results, "Antigravity produced no result event"
    assert results[-1].get("status") == "SUCCESS", results[-1]
    response = results[-1].get("response")
    assert isinstance(response, str), results[-1]
    return response


def _logs(session) -> list[Path]:
    return sorted((session.home / STATE_DIRECTORY / "log").glob("cli-*.log"))


def requested_urls(session) -> list[str]:
    """Every model request URL Antigravity logged in this session home."""
    urls = [
        match.group(1)
        for path in _logs(session)
        for match in _REQUEST.finditer(path.read_text(errors="replace"))
    ]
    session.record("antigravity-requests.json", {"urls": urls})
    return urls


def requested_models(session) -> set[str]:
    """Model names Antigravity sent; ug's proxy maps them to Databricks endpoints afterwards."""
    return {
        url.split("/models/", 1)[1].split(":", 1)[0]
        for url in requested_urls(session)
        if "/models/" in url
    }


def gateway_authenticated(session) -> bool:
    """Antigravity reports gateway auth rather than a Google account sign-in."""
    return any(
        "authenticated via gateway" in path.read_text(errors="replace") for path in _logs(session)
    )


def answer_contains(session, value: str) -> bool:
    """Whether a finished agent response in Antigravity's own store contains ``value``."""
    needle = value.encode()
    for path in (session.home / STATE_DIRECTORY / "conversations").glob("*.db"):
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "SELECT step_payload FROM steps WHERE step_type = ? AND status = ?",
                (AGENT_RESPONSE_STEP, DONE_STATUS),
            ).fetchall()
        except sqlite3.Error:
            continue
        finally:
            connection.close()
        if any(isinstance(payload, bytes) and needle in payload for (payload,) in rows):
            return True
    return False
