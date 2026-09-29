from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

import pytest

from scripts import diagnose_claude_routing as diag
from ucode.smart_routing.v2 import _routed_claude_agent_name


@pytest.mark.parametrize(
    "model",
    [
        "system.ai.kimi-k3",
        "system.ai.glm-5-3-flash",
        "databricks-claude-sonnet-4-6",
        "system.ai.claude-opus-4-8[1m]",
        "anthropic-aigw-73ea02b2-system.ai.glm-5-2",
        "catalog.schema.Model_Name",
    ],
)
def test_standalone_name_algorithm_matches_launcher(model):
    assert diag.routed_name(model) == _routed_claude_agent_name(model).rpartition(":")[-1]


def test_settings_allowlist_excludes_secrets_and_arbitrary_hook_commands():
    doc = {
        "apiKeyHelper": "echo PRIVATE_TOKEN",
        "permissions": {"allow": ["Agent(Explore)", "Bash(echo PRIVATE_TOKEN)"], "deny": ["Task"]},
        "env": {"OAUTH_TOKEN": "PRIVATE_TOKEN", "ENABLE_SMART_ROUTING_SUBAGENT_ONLY": "1"},
        "modelPicker": {"options": [{"model": "system.ai.kimi-k3", "secret": "PRIVATE_TOKEN"}]},
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Agent|Task",
                    "hooks": [
                        {"type": "command", "command": "echo PRIVATE_TOKEN"},
                        {
                            "type": "command",
                            "command": shlex.join(
                                [
                                    "/path with spaces/ug",
                                    "claude-router-hook",
                                    "route-subagent",
                                    "--host",
                                    "https://user:PRIVATE_TOKEN@example",
                                    "--model",
                                    "system.ai.kimi-k3",
                                ]
                            ),
                        },
                    ],
                }
            ]
        },
    }
    result = diag.summarize_settings(doc)
    assert "PRIVATE_TOKEN" not in json.dumps(result)
    assert result["hooks"][1]["executable"] == "/path with spaces/ug"
    assert result["hooks"][1]["models"] == ["system.ai.kimi-k3"]
    assert result["env"] == {"ENABLE_SMART_ROUTING_SUBAGENT_ONLY": "1"}
    assert result["agent_permissions"] == {"allow": ["Agent(Explore)"], "deny": ["Task"]}


def test_agent_arguments_preserve_models_but_not_prompts():
    result = diag.summarize_agents(
        json.dumps(
            {
                "custom": {
                    "model": "system.ai.kimi-k3",
                    "prompt": "PRIVATE_PROMPT",
                    "description": "PRIVATE_PROMPT",
                }
            }
        )
    )
    assert result == {"status": "ok", "agents": {"custom": {"model": "system.ai.kimi-k3"}}}
    assert diag.summarize_agents("{")["status"] == "invalid_json"
    assert diag.summarize_agents("[]")["status"] == "not_an_object"


def test_registry_comparison_distinguishes_missing_mismatch_and_unavailable():
    model = "system.ai.kimi-k3"
    settings = [
        {
            "path": "launch.json",
            "scope": "launch",
            "settings": {"hooks": [{"routing_event": "route-subagent", "models": [model]}]},
        }
    ]
    process = {"status": "ok", "agents_arguments": []}
    result = diag.compare_registry(process, settings)[0]
    assert result["missing_agents"] == ["ucode-route-kimi-k3-ba377e31"]
    process["agents_arguments"] = [
        diag.summarize_agents(json.dumps({diag.routed_name(model): {"model": model}}))
    ]
    assert diag.compare_registry(process, settings)[0]["status"] == "match"
    process["agents_arguments"][0]["agents"][diag.routed_name(model)]["model"] = "wrong"
    assert diag.compare_registry(process, settings)[0]["model_mismatches"] == [
        diag.routed_name(model)
    ]
    assert diag.compare_registry(None, settings)[0]["status"] == "unknown"

    # New launches use plugins, not --agents; don't falsely report missing agents.
    process["agents_arguments"] = []
    process["launch_options"] = {"--plugin-dir": ["/launch/plugin"]}
    findings = diag.compare_registry(process, settings)
    assert all(finding["status"] == "unknown" for finding in findings)
    assert findings[1]["missing_agents"] == []


def test_process_selection_does_not_guess_between_other_sessions():
    table = {
        1: {"pid": 1, "ppid": 0, "executable": "init"},
        5: {"pid": 5, "ppid": 1, "executable": "claude"},
        6: {"pid": 6, "ppid": 5, "executable": "bash"},
        7: {"pid": 7, "ppid": 1, "executable": "claude"},
    }
    assert diag.select_process(table, None, 6) == 5
    assert diag.select_process(table, None, 1) is None
    assert diag.select_process(table, 7, 6) == 7


def test_macos_process_arguments_preserve_json_spaces_and_empty_arguments():
    argv = ["claude", "--agents", '{"custom": {"model": "system.ai.kimi-k3"}}', ""]
    raw = (
        len(argv).to_bytes(4, sys.byteorder)
        + b"/path/claude\0\0"
        + "\0".join(argv).encode()
        + b"\0OAUTH_TOKEN=PRIVATE_TOKEN\0ENABLE_SMART_ROUTING_SUBAGENT_ONLY=1\0"
    )
    args, env = diag.parse_mac_process_data(raw)
    assert args == argv
    assert env["ENABLE_SMART_ROUTING_SUBAGENT_ONLY"] == "1"


def test_disappeared_process_is_partial_result(monkeypatch):
    monkeypatch.setattr(diag.sys, "platform", "linux")
    monkeypatch.setattr(diag, "run_readonly", lambda _: "")
    result, args, env = diag.process_data(999999999)
    assert result["status"] == "FileNotFoundError"
    assert args == []
    assert env == {}


@pytest.mark.parametrize("namespace", ["", "ucode-smart-routing:"])
def test_log_extraction_requires_real_tool_errors_and_matching_session(tmp_path, namespace):
    error = f"Agent type '{namespace}ucode-route-kimi-k3-ba377e31' not found. Available agents: Explore, general-purpose"

    def row(session, role, content):
        return {
            "sessionId": session,
            "timestamp": "2026-09-29T00:00:00Z",
            "message": {"role": role, "content": content},
        }

    rows = [
        row("chosen", "user", [{"type": "text", "text": error}]),
        row("chosen", "assistant", [{"type": "text", "text": error}]),
        row("other", "user", [{"type": "tool_result", "is_error": True, "content": error}]),
        row(
            "chosen",
            "assistant",
            [
                {
                    "type": "tool_use",
                    "name": "Agent",
                    "id": "tool-1",
                    "input": {"subagent_type": "Explore", "prompt": "PRIVATE_PROMPT"},
                }
            ],
        ),
        row(
            "chosen",
            "user",
            [{"type": "tool_result", "tool_use_id": "tool-1", "is_error": True, "content": error}],
        ),
    ]
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + '\n{"truncated"')
    result = diag.log_events(path, "chosen", transcript=True)
    assert result["malformed_lines"] == 1
    assert [r["event"] for r in result["events"]] == ["spawn_call", "agent_type_not_found"]
    assert result["events"][1]["available_agents"] == ["Explore", "general-purpose"]
    assert "PRIVATE_PROMPT" not in json.dumps(result)


def test_decision_logs_exclude_task_and_rationale(tmp_path):
    path = tmp_path / "decisions.jsonl"
    path.write_text(
        json.dumps(
            {
                "session_id": "chosen",
                "requested_model": "system.ai.kimi-k3",
                "task_name": "PRIVATE_TASK",
                "rationale": "PRIVATE_TASK",
            }
        )
        + "\n"
    )
    result = diag.log_events(path, "chosen")
    assert result["events"] == [{"session_id": "chosen", "requested_model": "system.ai.kimi-k3"}]


def test_bounded_tail_reports_truncation_and_retains_complete_last_event(tmp_path, monkeypatch):
    monkeypatch.setattr(diag, "LIMIT", 100)
    path = tmp_path / "decisions.jsonl"
    path.write_text("x" * 200 + '\n{"session_id":"chosen","model":"kimi"}\n')
    result = diag.log_events(path, "chosen")
    assert result["tail_truncated"] is True
    assert result["events"] == [{"session_id": "chosen", "model": "kimi"}]


@pytest.mark.parametrize("raw,status", [("{", "invalid_json"), ("[]", "not_an_object")])
def test_bad_settings_are_reported_without_exposing_contents(tmp_path, raw, status):
    path = tmp_path / "settings.json"
    path.write_text(raw)
    meta, doc = diag.read_json(path)
    assert meta["status"] == status
    assert doc == {}


def test_main_writes_private_report_and_refuses_existing_directory(tmp_path, monkeypatch):
    output = tmp_path / "report"
    report = {
        "collected_at": "now",
        "findings": [],
        "logs": [],
        "limitations": [],
        "path": str(Path.home() / "private"),
    }
    monkeypatch.setattr(diag, "collect", lambda *_: report)
    monkeypatch.setattr(sys, "argv", ["diagnose", "--output", str(output)])
    assert diag.main() == 0
    assert str(Path.home()) not in (output / "report.json").read_text()
    assert os.stat(output).st_mode & 0o777 == 0o700
    assert os.stat(output / "report.json").st_mode & 0o777 == 0o600
    before = (output / "report.json").read_bytes()
    assert diag.main() == 1
    assert (output / "report.json").read_bytes() == before


def test_subagent_evidence_records_type_and_response_model_without_content(tmp_path):
    parent = tmp_path / "session.jsonl"
    root = tmp_path / "session/subagents"
    root.mkdir(parents=True)
    (root / "agent-1.meta.json").write_text(
        json.dumps(
            {
                "agentType": "ucode-route-kimi-k3-ba377e31",
                "toolUseId": "call-1",
                "description": "PRIVATE_DESCRIPTION",
            }
        )
    )
    rows = [
        {"message": {"role": "assistant", "model": "kimi-backend", "content": "PRIVATE_MESSAGE"}},
        {
            "sessionId": "session",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "is_error": True,
                        "content": "Agent type 'ucode-route-nested' not found. Available agents: Explore",
                    }
                ],
            },
        },
    ]
    (root / "agent-1.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n{broken")
    result = diag.subagent_evidence(parent)
    assert result["agents"][0]["agentType"] == "ucode-route-kimi-k3-ba377e31"
    assert result["agents"][0]["response_models"] == ["kimi-backend"]
    assert (
        result["agents"][0]["tool_evidence"]["events"][0]["missing_agent"] == "ucode-route-nested"
    )
    assert "PRIVATE" not in json.dumps(result)


def test_wrapped_available_agent_list_excludes_trailing_error_details(tmp_path):
    content = "Agent type 'ucode-route-test' not found. Available agents: Explore,\n general-purpose\nPRIVATE_TRAILING_DETAILS"
    path = tmp_path / "session.jsonl"
    path.write_text(
        json.dumps(
            {
                "sessionId": "chosen",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "is_error": True, "content": content}],
                },
            }
        )
    )
    result = diag.log_events(path, "chosen", transcript=True)
    assert result["events"][0]["available_agents"] == ["Explore", "general-purpose"]
    assert "PRIVATE" not in json.dumps(result)


def test_collector_reads_only_local_evidence_and_survives_missing_config(tmp_path, monkeypatch):
    monkeypatch.setattr(diag, "process_table", lambda: {})
    monkeypatch.setattr(diag, "run_readonly", lambda _: pytest.fail("unexpected command"))
    monkeypatch.setattr(diag.shutil, "which", lambda *args, **kwargs: None)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    before = set(tmp_path.rglob("*"))
    result = diag.collect(session_id="26ac12ed-8cc1-4b4a-b1f9-5944dfb0aba2")
    assert set(tmp_path.rglob("*")) == before
    assert "process" not in result
    assert result["logs"][0]["status"] == "FileNotFoundError"
    assert result["findings"] == [{"check": "launch_settings", "status": "unavailable"}]
