from __future__ import annotations

import csv
import json
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from ucode import subagent_usage
from ucode.agents import claude, claude_subagent_usage
from ucode.cli import app


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8")


def _transcripts(tmp_path: Path, agent_id: str = "agent-1") -> tuple[Path, Path]:
    prompt = "Inspect the parser"
    parent = tmp_path / "parent.jsonl"
    child = tmp_path / "child.jsonl"
    usage_value = {
        "input_tokens": 2,
        "cache_creation_input_tokens": 3,
        "cache_read_input_tokens": 5,
        "output_tokens": 7,
    }
    _write_jsonl(
        child,
        [
            {
                "type": "user",
                "timestamp": "2026-09-22T10:00:01Z",
                "message": {"content": prompt},
            },
            {
                "type": "assistant",
                "uuid": "child-thinking",
                "message": {
                    "id": "response-1",
                    "model": "system.ai.claude-sonnet-5",
                    "usage": usage_value,
                    "content": [{"type": "thinking", "thinking": "..."}],
                },
            },
            {
                "type": "assistant",
                "uuid": "child-text",
                "message": {
                    "id": "response-1",
                    "model": "system.ai.claude-sonnet-5",
                    "usage": usage_value,
                    "content": [{"type": "text", "text": "Done"}],
                },
            },
        ],
    )
    _write_jsonl(
        parent,
        [
            {
                "type": "assistant",
                "timestamp": "2026-09-22T10:00:00Z",
                "message": {
                    "id": "parent-response",
                    "model": "system.ai.claude-opus-5",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Agent",
                            "id": "tool-1",
                            "input": {"prompt": prompt, "subagent_type": "Explore"},
                        }
                    ],
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool-1",
                            "content": f"Async agent launched. agentId: {agent_id}",
                        }
                    ]
                },
            },
        ],
    )
    return parent, child


def _payload(parent: Path, child: Path) -> dict:
    return {
        "session_id": "session-1",
        "agent_id": "agent-1",
        "agent_type": "Explore",
        "transcript_path": str(parent),
        "agent_transcript_path": str(child),
    }


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_builds_and_records_claude_row(tmp_path, monkeypatch):
    monkeypatch.setattr(
        subagent_usage,
        "usage_directory",
        lambda subdirectory: tmp_path / "usage" / subdirectory,
    )
    parent, child = _transcripts(tmp_path)

    row = claude_subagent_usage.ClaudeSubagentUsageRow.build(
        _payload(parent, child), now=1_800_000_000
    )
    path = claude_subagent_usage.ClaudeSubagentUsageRow.record(
        _payload(parent, child), now=1_800_000_000
    )

    assert isinstance(row, claude_subagent_usage.ClaudeSubagentUsageRow)
    assert isinstance(row, subagent_usage.SubagentUsageRow)
    assert path is not None
    assert path.parent == tmp_path / "usage" / "claude"
    assert _read_rows(path) == [
        {
            "recorded_at_utc": "2027-01-15T08:00:00+00:00",
            "session_id": "session-1",
            "agent_id": "agent-1",
            "subagent_name": "Explore",
            "main_model": "system.ai.claude-opus-5",
            "subagent_model": "system.ai.claude-sonnet-5",
            "input_tokens": "2",
            "cache_creation_input_tokens": "3",
            "cache_read_input_tokens": "5",
            "output_tokens": "7",
            "total_tokens": "17",
            "status": "ok",
        }
    ]


def test_falls_back_to_prompt_before_parent_receives_result(tmp_path):
    parent, child = _transcripts(tmp_path)
    parent_rows = [json.loads(line) for line in parent.read_text().splitlines()]
    _write_jsonl(parent, parent_rows[:1])

    row = claude_subagent_usage.ClaudeSubagentUsageRow.build(
        _payload(parent, child), now=1_800_000_000
    )

    assert row is not None
    assert row.main_model == "system.ai.claude-opus-5"
    assert row.status == "partial:exact_parent_link"


def test_missing_transcripts_build_partial_row(tmp_path):
    row = claude_subagent_usage.ClaudeSubagentUsageRow.build(
        _payload(tmp_path / "missing-parent", tmp_path / "missing-child"),
        now=1_800_000_000,
    )

    assert row is not None
    assert row.total_tokens == 0
    assert row.status == "partial:usage|subagent_model|main_model"


def test_hook_config_preserves_user_hooks():
    settings = {
        "hooks": {
            "SubagentStop": [{"hooks": [{"type": "command", "command": "user-subagent-stop"}]}]
        }
    }

    claude_subagent_usage.sync_hook(settings, hook_enabled=True)
    commands = [
        hook["command"] for group in settings["hooks"]["SubagentStop"] for hook in group["hooks"]
    ]
    assert "user-subagent-stop" in commands
    assert any(claude_subagent_usage.HOOK_COMMAND_MARKER in command for command in commands)

    claude_subagent_usage.sync_hook(settings, hook_enabled=False)
    assert settings["hooks"]["SubagentStop"] == [
        {"hooks": [{"type": "command", "command": "user-subagent-stop"}]}
    ]


def test_normal_and_relayed_launches_receive_transient_hook(monkeypatch):
    monkeypatch.setenv(subagent_usage.ENABLE_SUBAGENT_USAGE_CSV, "1")
    monkeypatch.setattr(claude, "read_json_safe", lambda _path: {"env": {}})

    normal = claude._build_claude_argv("claude", ["-p", "hi"])
    relayed = claude._build_claude_argv("claude", ["-p", "hi"], relayed=True)

    for argv in (normal, relayed):
        settings = json.loads(argv[argv.index("--settings") + 1])
        command = settings["hooks"]["SubagentStop"][0]["hooks"][0]["command"]
        assert claude_subagent_usage.HOOK_COMMAND_MARKER in command
    assert "--setting-sources" in relayed


def test_disabled_launch_removes_stale_hook_and_preserves_user_hook(monkeypatch):
    stale_settings = {
        "hooks": {
            "SubagentStop": [
                {
                    "hooks": [
                        {"type": "command", "command": "user-subagent-stop"},
                        {
                            "type": "command",
                            "command": f"ug {claude_subagent_usage.HOOK_COMMAND_MARKER}",
                        },
                    ]
                }
            ]
        }
    }
    monkeypatch.setattr(claude, "read_json_safe", lambda _path: stale_settings)

    for flag_value in (None, "0"):
        if flag_value is None:
            monkeypatch.delenv(subagent_usage.ENABLE_SUBAGENT_USAGE_CSV, raising=False)
        else:
            monkeypatch.setenv(subagent_usage.ENABLE_SUBAGENT_USAGE_CSV, flag_value)
        argv = claude._build_claude_argv("claude", ["-p", "hi"])
        settings = json.loads(argv[argv.index("--settings") + 1])
        commands = [
            hook["command"]
            for group in settings["hooks"]["SubagentStop"]
            for hook in group["hooks"]
        ]
        assert commands == ["user-subagent-stop"]


def test_enabled_launch_replaces_stale_hook_with_one_current_hook(monkeypatch):
    monkeypatch.setenv(subagent_usage.ENABLE_SUBAGENT_USAGE_CSV, "1")
    monkeypatch.setattr(
        claude,
        "read_json_safe",
        lambda _path: {
            "hooks": {
                "SubagentStop": [
                    {
                        "hooks": [
                            {"type": "command", "command": "user-subagent-stop"},
                            {
                                "type": "command",
                                "command": f"old-ug {claude_subagent_usage.HOOK_COMMAND_MARKER}",
                            },
                        ]
                    }
                ]
            }
        },
    )

    argv = claude._build_claude_argv("claude", ["-p", "hi"])
    settings = json.loads(argv[argv.index("--settings") + 1])
    commands = [
        hook["command"] for group in settings["hooks"]["SubagentStop"] for hook in group["hooks"]
    ]
    assert "user-subagent-stop" in commands
    assert sum(claude_subagent_usage.HOOK_COMMAND_MARKER in command for command in commands) == 1
    assert all(not command.startswith("old-ug") for command in commands)


def test_hidden_hook_is_silent_and_fail_open():
    runner = CliRunner()
    payload = {"session_id": "session-1", "agent_id": "agent-1"}
    with patch.object(claude_subagent_usage.ClaudeSubagentUsageRow, "record") as record:
        result = runner.invoke(
            app,
            [claude_subagent_usage.HOOK_COMMAND_MARKER],
            input=json.dumps(payload),
            env={subagent_usage.ENABLE_SUBAGENT_USAGE_CSV: "1"},
        )

    assert result.exit_code == 0
    assert result.output == ""
    record.assert_called_once_with(payload)

    invalid = runner.invoke(
        app,
        [claude_subagent_usage.HOOK_COMMAND_MARKER],
        input="not json",
        env={subagent_usage.ENABLE_SUBAGENT_USAGE_CSV: "1"},
    )
    assert invalid.exit_code == 0
    assert invalid.output == ""
