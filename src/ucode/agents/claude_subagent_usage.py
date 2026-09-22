"""Claude Code hook adapter for the harness-agnostic subagent usage writer."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ucode import subagent_usage
from ucode.databricks import ug_binary
from ucode.smart_routing import hooks

HOOK_COMMAND_MARKER = "claude-subagent-usage-hook"


def sync_hook(settings: dict, *, hook_enabled: bool) -> None:
    groups: dict[str, list[dict]] = {}
    if hook_enabled:
        argv = [ug_binary(), HOOK_COMMAND_MARKER]
        command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
        groups = {
            "SubagentStop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": command,
                            "timeout": 10,
                        }
                    ]
                }
            ]
        }
    hooks.sync_managed_hooks(settings, HOOK_COMMAND_MARKER, groups)


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], bool]:
    rows: list[dict[str, Any]] = []
    complete = True
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    complete = False
                    continue
                if isinstance(value, dict):
                    rows.append(value)
    except OSError:
        return [], False
    return rows, complete


def _integer(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _usage(records: Iterable[dict[str, Any]]) -> tuple[dict[str, int], list[str]]:
    unique: dict[str, dict[str, Any]] = {}
    for record in records:
        message = record.get("message")
        if record.get("type") != "assistant" or not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        identity = message.get("id") or record.get("uuid")
        if isinstance(identity, str) and identity not in unique:
            unique[identity] = message

    totals = {
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
    }
    models: list[str] = []
    for message in unique.values():
        usage = message["usage"]
        for field in totals:
            totals[field] += _integer(usage.get(field))
        model = message.get("model")
        if isinstance(model, str) and model and model not in models:
            models.append(model)
    return totals, models


def _content_text(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _content_text(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _content_text(item)


def _first_child_prompt(records: Iterable[dict[str, Any]]) -> tuple[str | None, str | None]:
    for record in records:
        message = record.get("message")
        if record.get("type") != "user" or not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str) and content:
            timestamp = record.get("timestamp")
            return content, timestamp if isinstance(timestamp, str) else None
    return None, None


def _timestamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _parent_model(
    records: Iterable[dict[str, Any]],
    *,
    agent_id: str,
    agent_type: str,
    child_prompt: str | None,
    child_timestamp: str | None,
) -> tuple[str | None, bool]:
    spawns: dict[str, dict[str, Any]] = {}
    results: list[tuple[str, str]] = []
    for record in records:
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if record.get("type") == "assistant" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = block.get("name")
                tool_id = block.get("id")
                tool_input = block.get("input")
                if (
                    not isinstance(name, str)
                    or name.casefold() not in {"agent", "task"}
                    or not isinstance(tool_id, str)
                    or not isinstance(tool_input, dict)
                ):
                    continue
                spawns[tool_id] = {
                    "model": message.get("model"),
                    "prompt": tool_input.get("prompt"),
                    "agent_type": tool_input.get("subagent_type"),
                    "timestamp": record.get("timestamp"),
                }
        elif record.get("type") == "user" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tool_id = block.get("tool_use_id")
                if isinstance(tool_id, str):
                    results.append((tool_id, "\n".join(_content_text(block.get("content")))))

    for tool_id, result_text in results:
        if agent_id in result_text and tool_id in spawns:
            model = spawns[tool_id].get("model")
            return (model if isinstance(model, str) and model else None), True

    candidates = [
        spawn
        for spawn in spawns.values()
        if child_prompt is not None
        and spawn.get("prompt") == child_prompt
        and (not spawn.get("agent_type") or spawn.get("agent_type") == agent_type)
    ]
    if candidates:
        child_at = _timestamp(child_timestamp)

        def distance(spawn: dict[str, Any]) -> float:
            spawn_at = _timestamp(spawn.get("timestamp"))
            if child_at is None or spawn_at is None:
                return 0
            return abs(child_at - spawn_at)

        model = min(candidates, key=distance).get("model")
        return (model if isinstance(model, str) and model else None), False
    return None, False


def build_from_claude(
    payload: Mapping[str, Any], *, now: float | None = None
) -> subagent_usage.SubagentUsageRow | None:
    session_id = payload.get("session_id")
    agent_id = payload.get("agent_id")
    if (
        not isinstance(session_id, str)
        or not session_id
        or not isinstance(agent_id, str)
        or not agent_id
    ):
        return None
    agent_type = payload.get("agent_type")
    subagent_name = agent_type if isinstance(agent_type, str) else ""
    child_path = payload.get("agent_transcript_path")
    parent_path = payload.get("transcript_path")
    child_records, child_complete = (
        _read_jsonl(Path(child_path).expanduser())
        if isinstance(child_path, str) and child_path
        else ([], False)
    )
    parent_records, parent_complete = (
        _read_jsonl(Path(parent_path).expanduser())
        if isinstance(parent_path, str) and parent_path
        else ([], False)
    )

    totals, models = _usage(child_records)
    child_prompt, child_timestamp = _first_child_prompt(child_records)
    main_model, exact_parent_link = _parent_model(
        parent_records,
        agent_id=agent_id,
        agent_type=subagent_name,
        child_prompt=child_prompt,
        child_timestamp=child_timestamp,
    )
    missing: list[str] = []
    if not child_complete or not sum(totals.values()):
        missing.append("usage")
    if not models:
        missing.append("subagent_model")
    if not parent_complete or not main_model:
        missing.append("main_model")
    elif not exact_parent_link:
        missing.append("exact_parent_link")

    recorded_at = now if now is not None else time.time()
    return subagent_usage.SubagentUsageRow(
        recorded_at_utc=datetime.fromtimestamp(recorded_at, UTC).isoformat(),
        session_id=session_id,
        agent_id=agent_id,
        subagent_name=subagent_name,
        main_model=main_model or "",
        subagent_model="|".join(models),
        input_tokens=totals["input_tokens"],
        cache_creation_input_tokens=totals["cache_creation_input_tokens"],
        cache_read_input_tokens=totals["cache_read_input_tokens"],
        output_tokens=totals["output_tokens"],
        total_tokens=sum(totals.values()),
        status="ok" if not missing else f"partial:{'|'.join(missing)}",
    )


def record(payload: Mapping[str, Any], *, now: float | None = None) -> Path | None:
    recorded_at = now if now is not None else time.time()
    row = build_from_claude(payload, now=recorded_at)
    if row is None:
        return None
    return subagent_usage.write_subagent_usage(row, now=recorded_at)
