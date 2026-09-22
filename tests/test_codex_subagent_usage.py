from __future__ import annotations

import csv
import json
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from ucode import subagent_usage
from ucode.agents import LaunchOptions, codex, codex_subagent_usage
from ucode.cli import app


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8")


def _transcripts(tmp_path: Path, agent_id: str = "agent-1") -> tuple[Path, Path]:
    parent = tmp_path / "parent.jsonl"
    child = tmp_path / "child.jsonl"
    agent_path = "/root/inspect_parser"
    _write_jsonl(
        child,
        [
            {
                "timestamp": "2026-09-22T10:00:01Z",
                "type": "session_meta",
                "payload": {
                    "source": {
                        "subagent": {
                            "thread_spawn": {
                                "parent_thread_id": "parent-thread",
                                "agent_path": agent_path,
                                "agent_nickname": "Ada",
                            }
                        }
                    }
                },
            },
            {
                "type": "turn_context",
                "payload": {"model": "system.ai.gpt-5-6-luna"},
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 37,
                            "cached_input_tokens": 11,
                            "cache_write_input_tokens": 13,
                            "output_tokens": 17,
                            "reasoning_output_tokens": 7,
                            "total_tokens": 54,
                        }
                    },
                },
            },
        ],
    )
    _write_jsonl(
        parent,
        [
            {
                "type": "turn_context",
                "payload": {"model": "system.ai.gpt-6-astra"},
            },
            {
                "timestamp": "2026-09-22T10:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "spawn_agent",
                    "call_id": "call-1",
                    "arguments": json.dumps({"task_name": "inspect_parser"}),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": json.dumps({"agent_id": agent_id, "task_name": agent_path}),
                },
            },
        ],
    )
    return parent, child


def _payload(parent: Path, child: Path) -> dict:
    return {
        "session_id": "session-1",
        "agent_id": "agent-1",
        "agent_type": "worker",
        "model": "hook-fallback-model",
        "transcript_path": str(parent),
        "agent_transcript_path": str(child),
    }


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_builds_and_records_codex_row(tmp_path, monkeypatch):
    monkeypatch.setattr(subagent_usage, "usage_directory", lambda: tmp_path / "usage")
    parent, child = _transcripts(tmp_path)

    row = codex_subagent_usage.CodexSubagentUsageRow.build(
        _payload(parent, child), now=1_800_000_000
    )
    path = codex_subagent_usage.CodexSubagentUsageRow.record(
        _payload(parent, child), now=1_800_000_000
    )

    assert isinstance(row, codex_subagent_usage.CodexSubagentUsageRow)
    assert isinstance(row, subagent_usage.SubagentUsageRow)
    assert path is not None
    assert _read_rows(path) == [
        {
            "recorded_at_utc": "2027-01-15T08:00:00+00:00",
            "session_id": "session-1",
            "agent_id": "agent-1",
            "subagent_name": "worker",
            "main_model": "system.ai.gpt-6-astra",
            "subagent_model": "system.ai.gpt-5-6-luna",
            "input_tokens": "13",
            "cache_creation_input_tokens": "13",
            "cache_read_input_tokens": "11",
            "output_tokens": "17",
            "total_tokens": "54",
            "status": "ok",
        }
    ]


def test_falls_back_to_hook_model_and_marks_inexact_parent_link(tmp_path):
    parent, child = _transcripts(tmp_path)
    child_rows = [json.loads(line) for line in child.read_text().splitlines()]
    _write_jsonl(child, [child_rows[0], child_rows[2]])
    parent_rows = [json.loads(line) for line in parent.read_text().splitlines()]
    _write_jsonl(parent, parent_rows[:2])

    row = codex_subagent_usage.CodexSubagentUsageRow.build(
        _payload(parent, child), now=1_800_000_000
    )

    assert row is not None
    assert row.subagent_model == "hook-fallback-model"
    assert row.main_model == "system.ai.gpt-6-astra"
    assert row.status == "partial:exact_parent_link"


def test_missing_transcripts_build_partial_row(tmp_path):
    payload = _payload(tmp_path / "missing-parent", tmp_path / "missing-child")
    payload.pop("model")

    row = codex_subagent_usage.CodexSubagentUsageRow.build(payload, now=1_800_000_000)

    assert row is not None
    assert row.total_tokens == 0
    assert row.status == "partial:usage|subagent_model|main_model"


def test_hook_config_preserves_user_hooks():
    settings = {
        "hooks": {"SubagentStop": [{"hooks": [{"type": "command", "command": "user-hook"}]}]}
    }

    codex_subagent_usage.sync_hook(settings, hook_enabled=True)
    commands = [
        hook["command"] for group in settings["hooks"]["SubagentStop"] for hook in group["hooks"]
    ]
    assert "user-hook" in commands
    assert any(codex_subagent_usage.HOOK_COMMAND_MARKER in command for command in commands)

    codex_subagent_usage.sync_hook(settings, hook_enabled=False)
    assert settings["hooks"]["SubagentStop"] == [
        {"hooks": [{"type": "command", "command": "user-hook"}]}
    ]


def test_hidden_hook_always_emits_json_and_fails_open():
    runner = CliRunner()
    payload = {"session_id": "session-1", "agent_id": "agent-1"}
    with patch.object(codex_subagent_usage.CodexSubagentUsageRow, "record") as record:
        result = runner.invoke(
            app,
            [codex_subagent_usage.HOOK_COMMAND_MARKER],
            input=json.dumps(payload),
            env={subagent_usage.ENABLE_SUBAGENT_USAGE_CSV: "1"},
        )

    assert result.exit_code == 0
    assert result.output == "{}\n"
    record.assert_called_once_with(payload)

    invalid = runner.invoke(
        app,
        [codex_subagent_usage.HOOK_COMMAND_MARKER],
        input="not json",
        env={subagent_usage.ENABLE_SUBAGENT_USAGE_CSV: "1"},
    )
    assert invalid.exit_code == 0
    assert invalid.output == "{}\n"


def test_normal_codex_launch_receives_transient_hook(tmp_path, monkeypatch):
    launches: list[list[str]] = []
    profile_path = tmp_path / "ucode.config.toml"
    profile_path.write_text('model_provider = "ucode-databricks"\n', encoding="utf-8")
    monkeypatch.setenv(subagent_usage.ENABLE_SUBAGENT_USAGE_CSV, "1")
    monkeypatch.delenv("ENABLE_SMART_ROUTING_V2", raising=False)
    monkeypatch.delenv("ENABLE_SMART_ROUTING_SUBAGENT_ONLY", raising=False)
    monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", profile_path)
    monkeypatch.setattr(codex, "clear_model_preferences", lambda state: False)
    monkeypatch.setattr(codex, "agent_version", lambda binary: "0.145.0")
    monkeypatch.setattr(codex, "get_databricks_token", lambda *_args, **_kwargs: "token")
    monkeypatch.setattr(codex, "exec_or_spawn", lambda argv: launches.append(argv))

    codex.launch({"workspace": "https://example.databricks.com"}, [], options=LaunchOptions())

    hook_override = next(arg for arg in launches[0] if arg.startswith("hooks.SubagentStop="))
    assert codex_subagent_usage.HOOK_COMMAND_MARKER in hook_override
