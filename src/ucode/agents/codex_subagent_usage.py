"""Codex hook adapter for the harness-agnostic subagent usage writer."""

from __future__ import annotations

import json
import shlex
import subprocess
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ucode.databricks import ug_binary
from ucode.smart_routing import hooks
from ucode.subagent_usage import SubagentUsageRow

HOOK_COMMAND_MARKER = "codex-subagent-usage-hook"


def sync_hook(settings: dict, *, hook_enabled: bool) -> None:
    groups: dict[str, list[dict]] = {}
    if hook_enabled:
        argv = [ug_binary(), HOOK_COMMAND_MARKER]
        groups = {
            "SubagentStop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": shlex.join(argv),
                            "command_windows": subprocess.list2cmdline(argv),
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


def _json_object(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _usage(records: Iterable[dict[str, Any]]) -> tuple[dict[str, int], list[str]]:
    cumulative: dict[str, Any] = {}
    models: list[str] = []
    for record in records:
        payload = record.get("payload")
        if record.get("type") == "turn_context" and isinstance(payload, dict):
            model = payload.get("model")
            if isinstance(model, str) and model and model not in models:
                models.append(model)
        if (
            record.get("type") == "event_msg"
            and isinstance(payload, dict)
            and payload.get("type") == "token_count"
        ):
            info = payload.get("info")
            total = info.get("total_token_usage") if isinstance(info, dict) else None
            if isinstance(total, dict):
                cumulative = total

    raw_input = _integer(cumulative.get("input_tokens"))
    cache_read = _integer(cumulative.get("cached_input_tokens"))
    cache_creation = _integer(cumulative.get("cache_write_input_tokens"))
    return {
        "input_tokens": max(0, raw_input - cache_read - cache_creation),
        "cache_creation_input_tokens": cache_creation,
        "cache_read_input_tokens": cache_read,
        "output_tokens": _integer(cumulative.get("output_tokens")),
    }, models


def _child_source(records: Iterable[dict[str, Any]]) -> tuple[str | None, str | None]:
    for record in records:
        payload = record.get("payload")
        if record.get("type") != "session_meta" or not isinstance(payload, dict):
            continue
        source = payload.get("source")
        subagent = source.get("subagent") if isinstance(source, dict) else None
        spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
        if not isinstance(spawn, dict):
            continue
        path = spawn.get("agent_path")
        timestamp = record.get("timestamp")
        return (
            path if isinstance(path, str) and path else None,
            timestamp if isinstance(timestamp, str) else None,
        )
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
    agent_path: str | None,
    child_timestamp: str | None,
) -> tuple[str | None, bool]:
    current_model: str | None = None
    spawns: dict[str, dict[str, Any]] = {}
    outputs: dict[str, dict[str, Any]] = {}
    for record in records:
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if record.get("type") == "turn_context":
            model = payload.get("model")
            if isinstance(model, str) and model:
                current_model = model
            continue
        if record.get("type") != "response_item":
            continue
        call_id = payload.get("call_id")
        if not isinstance(call_id, str):
            continue
        if payload.get("type") == "function_call":
            name = payload.get("name")
            if not isinstance(name, str) or not name.casefold().endswith("spawn_agent"):
                continue
            arguments = _json_object(payload.get("arguments"))
            spawns[call_id] = {
                "model": current_model,
                "task_name": arguments.get("task_name"),
                "timestamp": record.get("timestamp"),
            }
        elif payload.get("type") == "function_call_output":
            outputs[call_id] = _json_object(payload.get("output"))

    for call_id, spawn in spawns.items():
        output = outputs.get(call_id, {})
        output_task = output.get("task_name")
        if agent_id in json.dumps(output, sort_keys=True) or (
            agent_path is not None and isinstance(output_task, str) and output_task == agent_path
        ):
            model = spawn.get("model")
            return (model if isinstance(model, str) and model else None), True

    task_name = agent_path.rsplit("/", 1)[-1] if agent_path else None
    candidates = [spawn for spawn in spawns.values() if spawn.get("task_name") == task_name]
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


@dataclass(frozen=True, slots=True)
class CodexSubagentUsageRow(SubagentUsageRow):
    @staticmethod
    def build(
        payload: Mapping[str, Any], *, now: float | None = None
    ) -> CodexSubagentUsageRow | None:
        session_id = payload.get("session_id")
        agent_id = payload.get("agent_id")
        if (
            not isinstance(session_id, str)
            or not session_id
            or not isinstance(agent_id, str)
            or not agent_id
        ):
            return None
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
        payload_model = payload.get("model")
        if not models and isinstance(payload_model, str) and payload_model:
            models.append(payload_model)
        agent_path, child_timestamp = _child_source(child_records)
        main_model, exact_parent_link = _parent_model(
            parent_records,
            agent_id=agent_id,
            agent_path=agent_path,
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

        agent_type = payload.get("agent_type")
        recorded_at = now if now is not None else time.time()
        return CodexSubagentUsageRow(
            recorded_at_utc=datetime.fromtimestamp(recorded_at, UTC).isoformat(),
            session_id=session_id,
            agent_id=agent_id,
            subagent_name=agent_type if isinstance(agent_type, str) else "",
            main_model=main_model or "",
            subagent_model="|".join(models),
            input_tokens=totals["input_tokens"],
            cache_creation_input_tokens=totals["cache_creation_input_tokens"],
            cache_read_input_tokens=totals["cache_read_input_tokens"],
            output_tokens=totals["output_tokens"],
            total_tokens=sum(totals.values()),
            status="ok" if not missing else f"partial:{'|'.join(missing)}",
        )
