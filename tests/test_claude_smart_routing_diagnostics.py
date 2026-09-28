"""Tests for Claude smart-routing launch/hook diagnostics and log bounding."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

import ucode.agents.claude as agents_claude
from ucode.cli import app
from ucode.databricks import AnthropicModelCatalog
from ucode.smart_routing import claude_diagnostics, claude_hooks, claude_routing, routing, v2

runner = CliRunner()

SETTINGS_PATH = "/tmp/claude-v2-1.json"
ROUTED_AGENT = "ucode-route-claude-opus-4-8-1a2b3c4d"
GOOD_ARGV = [
    "/usr/local/bin/claude",
    "--settings",
    SETTINGS_PATH,
    "--agents",
    json.dumps({ROUTED_AGENT: {"model": "system.ai.claude-opus-4-8"}, "reviewer": {}}),
]


@pytest.fixture(autouse=True)
def _isolated_settings_sources(tmp_path, monkeypatch):
    """Keep home/cwd settings discovery and routing env flags out of each test."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(agents_claude, "_managed_settings_path", lambda: None)
    monkeypatch.delenv(v2.ENABLE_SMART_ROUTING_ENV_VAR, raising=False)
    monkeypatch.delenv(v2.ENABLE_SUBAGENT_ROUTING_ENV_VAR, raising=False)
    monkeypatch.delenv("UCODE_DEBUG", raising=False)


def _write_launch_record(monkeypatch, **overrides) -> dict:
    monkeypatch.setattr(claude_diagnostics, "ug_version", lambda: "test-1.0")
    record = {
        "launch_id": "lid-1",
        "launch_path": "subagent-only",
        "reason": "smart-routing launch",
        "catalog_source": "discovery",
        "model_ids": ["system.ai.claude-opus-4-8"],
        "agent_names": [ROUTED_AGENT, "reviewer"],
        "settings_path": SETTINGS_PATH,
        "ucode_version": "test-1.0",
        "at": 1.0,
    }
    record.update(overrides)
    routing._append_jsonl(claude_diagnostics.LAUNCHES_PATH, record)
    return record


class TestParseClaudeArgv:
    def test_inline_forms(self):
        parsed = claude_diagnostics.parse_claude_argv(
            ["claude", "--settings", SETTINGS_PATH, "--agents", '{"reviewer": {}}', "--resume"]
        )

        assert parsed == {
            "settings_path": SETTINGS_PATH,
            "has_agents_flag": True,
            "agent_names": ["reviewer"],
            "routed_agent_names": [],
            "resume": True,
        }

    def test_equals_forms(self):
        parsed = claude_diagnostics.parse_claude_argv(
            ["claude", f"--settings={SETTINGS_PATH}", '--agents={"reviewer": {}}', "-c"]
        )

        assert parsed["settings_path"] == SETTINGS_PATH
        assert parsed["has_agents_flag"] is True
        assert parsed["agent_names"] == ["reviewer"]
        assert parsed["resume"] is True

    def test_agents_file_path_form(self, tmp_path):
        agents_file = tmp_path / "agents.json"
        agents_file.write_text(json.dumps({"reviewer": {}, ROUTED_AGENT: {}}))

        parsed = claude_diagnostics.parse_claude_argv(["claude", "--agents", str(agents_file)])

        assert parsed["agent_names"] == ["reviewer", ROUTED_AGENT]
        assert parsed["routed_agent_names"] == [ROUTED_AGENT]

    def test_without_agents_or_resume(self):
        parsed = claude_diagnostics.parse_claude_argv(["claude"])

        assert parsed == {
            "settings_path": None,
            "has_agents_flag": False,
            "agent_names": [],
            "routed_agent_names": [],
            "resume": False,
        }


class TestFindClaudeArgv:
    @staticmethod
    def _walk(monkeypatch, procs, start_pid=100):
        monkeypatch.setattr(claude_diagnostics.os, "getppid", lambda: start_pid)
        monkeypatch.setattr(claude_diagnostics, "_proc_cmdline", lambda pid: procs[pid][0])
        monkeypatch.setattr(claude_diagnostics, "_proc_ppid", lambda pid: procs[pid][1])
        return claude_diagnostics.find_claude_argv()

    def test_walks_past_intermediate_shell(self, monkeypatch):
        claude_argv = ["/usr/local/bin/claude", "--settings", "/s.json", "--agents", "{}"]
        argv = self._walk(
            monkeypatch,
            {
                100: (["/bin/sh", "-c", "ug claude-router-hook route-subagent"], 101),
                101: (claude_argv, 1),
            },
        )

        assert argv == claude_argv

    def test_returns_none_without_claude_ancestor(self, monkeypatch):
        argv = self._walk(
            monkeypatch,
            {100: (["/bin/zsh"], 101), 101: (["tmux"], 1)},
        )

        assert argv is None

    def test_never_raises(self, monkeypatch):
        def _boom(_pid):
            raise RuntimeError("proc vanished")

        monkeypatch.setattr(claude_diagnostics.os, "getppid", lambda: 100)
        monkeypatch.setattr(claude_diagnostics, "_proc_cmdline", _boom)

        assert claude_diagnostics.find_claude_argv() is None


class TestSettingsFilesWithRouterHooks:
    def test_reports_only_files_containing_router_hooks(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        claude_dir = home / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "settings.json").write_text(
            json.dumps({"hooks": {"PreToolUse": [{"command": "ug claude-router-hook x"}]}})
        )
        (claude_dir / "settings.local.json").write_text("{}")
        managed = tmp_path / "managed-settings.json"
        managed.write_text("ug claude-router-hook session-start")
        extra = tmp_path / "extra-settings.json"
        extra.write_text("ug claude-router-hook route-subagent")
        monkeypatch.setattr(agents_claude, "_managed_settings_path", lambda: managed)

        found = claude_diagnostics.settings_files_with_router_hooks([str(extra)])

        assert str(claude_dir / "settings.json") in found
        assert str(managed) in found
        assert str(extra) in found
        assert not any("settings.local" in path for path in found)


class TestDiagnoseVerdicts:
    def test_ok(self, monkeypatch):
        _write_launch_record(monkeypatch)
        monkeypatch.setattr(claude_diagnostics, "find_claude_argv", lambda: GOOD_ARGV)

        diagnosis = claude_diagnostics.diagnose(
            launch_id="lid-1", session_payload={"session_id": "s1", "source": "startup"}
        )

        assert diagnosis["verdict"] == "ok"
        assert diagnosis["session_id"] == "s1"
        assert diagnosis["source"] == "startup"
        assert diagnosis["missing_agents"] == []

    def test_no_launch_id_means_stale_hook(self, monkeypatch):
        diagnosis = claude_diagnostics.diagnose(launch_id=None)

        assert diagnosis["verdict"] == "no_launch_id"
        assert "without --launch-id" in diagnosis["message"]

    def test_launch_record_missing(self, monkeypatch):
        _write_launch_record(monkeypatch)

        diagnosis = claude_diagnostics.diagnose(launch_id="lid-other")

        assert diagnosis["verdict"] == "launch_record_missing"
        assert "lid-other" in diagnosis["message"]

    def test_version_mismatch(self, monkeypatch):
        _write_launch_record(monkeypatch, ucode_version="older-ucode")

        diagnosis = claude_diagnostics.diagnose(launch_id="lid-1")

        assert diagnosis["verdict"] == "version_mismatch"
        assert "older-ucode" in diagnosis["message"]
        assert "test-1.0" in diagnosis["message"]

    def test_claude_process_not_found(self, monkeypatch):
        _write_launch_record(monkeypatch)
        monkeypatch.setattr(claude_diagnostics, "find_claude_argv", lambda: None)

        diagnosis = claude_diagnostics.diagnose(launch_id="lid-1")

        assert diagnosis["verdict"] == "claude_process_not_found"

    def test_settings_mismatch(self, monkeypatch):
        _write_launch_record(monkeypatch)
        argv = ["claude", "--settings", "/tmp/other.json", *GOOD_ARGV[3:]]

        diagnosis = claude_diagnostics.diagnose(launch_id="lid-1", argv=argv)

        assert diagnosis["verdict"] == "settings_mismatch"
        assert "/tmp/other.json" in diagnosis["message"]

    def test_agents_flag_missing(self, monkeypatch):
        _write_launch_record(monkeypatch)
        argv = ["claude", "--settings", SETTINGS_PATH]

        diagnosis = claude_diagnostics.diagnose(launch_id="lid-1", argv=argv)

        assert diagnosis["verdict"] == "agents_flag_missing"

    def test_agents_incomplete(self, monkeypatch):
        _write_launch_record(monkeypatch)
        argv = [
            "claude",
            "--settings",
            SETTINGS_PATH,
            "--agents",
            json.dumps({"reviewer": {}}),
        ]

        diagnosis = claude_diagnostics.diagnose(launch_id="lid-1", argv=argv)

        assert diagnosis["verdict"] == "agents_incomplete"
        assert diagnosis["missing_agents"] == [ROUTED_AGENT]
        assert ROUTED_AGENT in diagnosis["message"]

    def test_routed_agent_not_registered_is_incomplete(self, monkeypatch):
        _write_launch_record(monkeypatch)

        diagnosis = claude_diagnostics.diagnose(
            launch_id="lid-1", argv=GOOD_ARGV, routed_agent="ucode-route-ghost-00000000"
        )

        assert diagnosis["verdict"] == "agents_incomplete"
        assert "ucode-route-ghost-00000000" in diagnosis["missing_agents"]


class TestHookArgv:
    @pytest.mark.parametrize("event", ["route-subagent", "session-start", "record-subagent"])
    def test_launch_id_present_for_all_events(self, event):
        state = {
            "workspace": "https://example.com",
            "profile": "p",
            "launch_id": "lid-1",
            "claude_models": {"0": "system.ai.claude-opus-4-8"},
        }

        argv = claude_hooks._routing_hook_argv(state, event)

        assert argv[argv.index("--launch-id") + 1] == "lid-1"

    def test_launch_id_omitted_when_state_has_none(self):
        argv = claude_hooks._routing_hook_argv(
            {"workspace": "https://example.com"}, "session-start"
        )

        assert "--launch-id" not in argv


class TestLaunchRecord:
    def test_launch_claude_records_agents_payload(self, tmp_path, monkeypatch):
        user_settings = tmp_path / "settings.json"
        user_settings.write_text(json.dumps({"model": "opus"}))
        monkeypatch.setenv(v2.ENABLE_SUBAGENT_ROUTING_ENV_VAR, "1")
        monkeypatch.setattr(v2, "APP_DIR", tmp_path)
        monkeypatch.setattr(v2, "_model_picker_catalog", lambda: None)
        monkeypatch.setattr(v2, "get_databricks_token", lambda *_args, **_kwargs: "token")
        monkeypatch.setattr(v2, "build_auth_token_argv", lambda *_args, **_kwargs: ["ug"])
        monkeypatch.setattr(
            v2,
            "list_anthropic_model_catalog",
            lambda *_args: AnthropicModelCatalog(
                model_ids=["system.ai.claude-opus-4-8"], model_id_to_display_name={}
            ),
        )
        captured: dict = {}

        class FakeProcess:
            def __init__(self, argv, **_kwargs):
                captured["argv"] = argv
                captured["agents"] = json.loads(argv[argv.index("--agents") + 1])
                captured["settings_path"] = argv[argv.index("--settings") + 1]
                captured["settings"] = json.loads(
                    Path(captured["settings_path"]).read_text(encoding="utf-8")
                )

            def wait(self):
                return 0

        monkeypatch.setattr(v2.subprocess, "Popen", FakeProcess)

        with pytest.raises(SystemExit) as exc:
            v2.launch_claude(
                {"workspace": "https://example.com"},
                [],
                binary="claude",
                user_settings_path=user_settings,
                launch_model="opus",
                compose_settings=lambda _args: ({}, []),
                launch_model_args=lambda args, _model: args,
                model_name=lambda model: model,
            )

        assert exc.value.code == 0
        records = claude_diagnostics.recent_launches()
        assert len(records) == 1
        record = records[0]
        assert record["launch_path"] == "subagent-only"
        assert record["catalog_source"] == "discovery"
        assert record["agent_names"] == list(captured["agents"])
        assert record["settings_path"] == captured["settings_path"]
        route_command = next(
            hook["command"]
            for group in captured["settings"]["hooks"]["PreToolUse"]
            for hook in group["hooks"]
            if "route-subagent" in hook["command"]
        )
        assert f"--launch-id {record['launch_id']}" in route_command


class TestRouterHookCmd:
    def test_session_start_emits_system_message_when_not_ok(self, monkeypatch):
        monkeypatch.setenv(v2.ENABLE_SMART_ROUTING_ENV_VAR, "1")
        monkeypatch.setattr(
            claude_routing, "CANARY_PATH", claude_diagnostics.LAUNCHES_PATH.parent / "canary.json"
        )
        _write_launch_record(monkeypatch)

        result = runner.invoke(
            app,
            ["claude-router-hook", "session-start", "--launch-id", "lid-unknown"],
            input='{"session_id": "s1", "source": "startup"}',
        )

        assert result.exit_code == 0, result.output
        message = json.loads(result.output)["systemMessage"]
        assert message.startswith("Smart Routing check: launch_record_missing")
        assert str(claude_diagnostics.DIAGNOSTICS_PATH) in message
        assert claude_diagnostics.recent_diagnostics()[0]["verdict"] == "launch_record_missing"

    def test_session_start_silent_when_ok(self, monkeypatch):
        monkeypatch.setenv(v2.ENABLE_SMART_ROUTING_ENV_VAR, "1")
        monkeypatch.setattr(
            claude_routing, "CANARY_PATH", claude_diagnostics.LAUNCHES_PATH.parent / "canary.json"
        )
        _write_launch_record(monkeypatch)
        monkeypatch.setattr(claude_diagnostics, "find_claude_argv", lambda: GOOD_ARGV)

        result = runner.invoke(
            app,
            ["claude-router-hook", "session-start", "--launch-id", "lid-1"],
            input='{"session_id": "s1", "source": "startup"}',
        )

        assert result.exit_code == 0, result.output
        assert result.output == ""
        assert claude_diagnostics.recent_diagnostics()[0]["verdict"] == "ok"

    def test_route_subagent_appends_diagnostic_suffix(self, monkeypatch):
        monkeypatch.setenv(v2.ENABLE_SMART_ROUTING_ENV_VAR, "1")
        monkeypatch.setenv("OAUTH_TOKEN", "token")
        _write_launch_record(monkeypatch)
        monkeypatch.setattr(claude_diagnostics, "find_claude_argv", lambda: GOOD_ARGV)
        routed_agent = "ucode-route-ghost-00000000"
        routed = {
            "systemMessage": "routed",
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": {"prompt": "fix it", "subagent_type": routed_agent},
            },
        }
        from unittest.mock import patch

        with patch(
            "ucode.cli.smart_routing_v2.route_claude_pre_tool_use", return_value=routed
        ) as mock_route:
            result = runner.invoke(
                app,
                [
                    "claude-router-hook",
                    "route-subagent",
                    "--host",
                    "https://example.com",
                    "--model",
                    "system.ai.claude-opus-4-8",
                    "--launch-id",
                    "lid-1",
                ],
                input='{"tool_name":"Agent","tool_input":{"prompt":"fix it"}}',
            )

        assert result.exit_code == 0, result.output
        mock_route.assert_called_once()
        output = json.loads(result.output)
        assert output["systemMessage"].endswith(" [diagnostic: agents_incomplete]")
        assert output["hookSpecificOutput"]["updatedInput"]["subagent_type"] == routed_agent
        record = claude_diagnostics.recent_diagnostics()[0]
        assert record["routed_agent"] == routed_agent
        assert record["verdict"] == "agents_incomplete"

    def test_route_subagent_silent_when_agent_registered(self, monkeypatch):
        monkeypatch.setenv(v2.ENABLE_SMART_ROUTING_ENV_VAR, "1")
        monkeypatch.setenv("OAUTH_TOKEN", "token")
        monkeypatch.setattr(claude_diagnostics, "find_claude_argv", lambda: GOOD_ARGV)
        routed = {
            "systemMessage": "routed",
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": {"prompt": "fix it", "subagent_type": ROUTED_AGENT},
            },
        }
        from unittest.mock import patch

        with patch("ucode.cli.smart_routing_v2.route_claude_pre_tool_use", return_value=routed):
            result = runner.invoke(
                app,
                [
                    "claude-router-hook",
                    "route-subagent",
                    "--host",
                    "https://example.com",
                    "--model",
                    "system.ai.claude-opus-4-8",
                ],
                input='{"tool_name":"Agent","tool_input":{"prompt":"fix it"}}',
            )

        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == routed
        assert claude_diagnostics.recent_diagnostics() == []

    def test_doctor_bypasses_enable_gate_and_reports_latest_launch(self, monkeypatch):
        _write_launch_record(monkeypatch)
        monkeypatch.setattr(claude_diagnostics, "find_claude_argv", lambda: None)

        result = runner.invoke(app, ["claude-router-hook", "doctor"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["diagnosis"]["launch_id"] == "lid-1"
        assert payload["diagnosis"]["verdict"] == "claude_process_not_found"
        assert len(payload["recent_launches"]) == 1
        assert payload["recent_diagnostics"] == []
        assert "debug-logs" in payload["debug_logs_dir"]


class TestNotRoutedLaunchRecords:
    def test_cli_records_not_routed_reasons(self, monkeypatch):
        from ucode import cli

        cli._record_not_routed_launch(tool_args=["--model", "opus"], model=None)
        cli._record_not_routed_launch(tool_args=[], model="system.ai.glm-5-3")
        cli._record_not_routed_launch(tool_args=[], model=None)

        records = claude_diagnostics.recent_launches(limit=3)
        assert [record["reason"] for record in records] == [
            "explicit model",
            "explicit model",
            "non-interactive",
        ]
        assert all(record["launch_path"] == "not-routed" for record in records)
        assert all(record["agent_names"] == [] for record in records)


class TestBoundedLogs:
    def test_jsonl_append_rotates_past_threshold(self, tmp_path, monkeypatch):
        decisions = tmp_path / "decisions.jsonl"
        decisions.write_text("old-row-old-row\n", encoding="utf-8")
        monkeypatch.setattr(routing, "MAX_JSONL_BYTES", 8)

        routing.write_decision_record(
            decisions,
            {"session_id": "s1"},
            "fix the parser",
            routing.RoutingDecision(model="system.ai.claude-opus-4-8", raw_model="claude-opus-4-8"),
            "system.ai.claude-opus-4-8",
        )

        assert Path(f"{decisions}.1").read_text(encoding="utf-8") == "old-row-old-row\n"
        records = routing._read_jsonl(decisions)
        assert records[0]["session_id"] == "s1"
        assert records[0]["requested_model"] == "system.ai.claude-opus-4-8"

    def test_clear_artifacts_removes_rotations(self, tmp_path):
        canary = tmp_path / "canary.json"
        canary.write_text("{}", encoding="utf-8")
        Path(f"{canary}.1").write_text("{}", encoding="utf-8")

        routing.clear_artifacts((canary,))

        assert not canary.exists()
        assert not Path(f"{canary}.1").exists()

    def test_clear_routing_artifacts_removes_debug_logs(self, tmp_path, monkeypatch):
        debug_dir = tmp_path / "debug-logs" / "claude"
        debug_dir.mkdir(parents=True)
        launches = debug_dir / "smart-routing-launches.jsonl"
        diagnostics = debug_dir / "smart-routing-diagnostics.jsonl"
        launches.write_text("{}\n", encoding="utf-8")
        Path(f"{launches}.1").write_text("{}\n", encoding="utf-8")
        diagnostics.write_text("{}\n", encoding="utf-8")
        monkeypatch.setattr(claude_routing, "CANARY_PATH", tmp_path / "canary.json")
        monkeypatch.setattr(claude_routing, "AUDIT_PATH", tmp_path / "audit.jsonl")
        monkeypatch.setattr(claude_routing, "DECISIONS_PATH", tmp_path / "decisions.jsonl")
        monkeypatch.setattr(claude_diagnostics, "LAUNCHES_PATH", launches)
        monkeypatch.setattr(claude_diagnostics, "DIAGNOSTICS_PATH", diagnostics)
        monkeypatch.setattr(routing, "APP_DIR", tmp_path)

        claude_routing.clear_routing_artifacts()

        assert not launches.exists()
        assert not Path(f"{launches}.1").exists()
        assert not diagnostics.exists()
        assert not debug_dir.exists()
        assert not (tmp_path / "debug-logs").exists()

    def test_clear_routing_artifacts_keeps_other_harness_logs(self, tmp_path, monkeypatch):
        debug_dir = tmp_path / "debug-logs" / "claude"
        debug_dir.mkdir(parents=True)
        (tmp_path / "debug-logs" / "codex").mkdir()
        launches = debug_dir / "smart-routing-launches.jsonl"
        launches.write_text("{}\n", encoding="utf-8")
        monkeypatch.setattr(claude_routing, "CANARY_PATH", tmp_path / "canary.json")
        monkeypatch.setattr(claude_routing, "AUDIT_PATH", tmp_path / "audit.jsonl")
        monkeypatch.setattr(claude_routing, "DECISIONS_PATH", tmp_path / "decisions.jsonl")
        monkeypatch.setattr(claude_diagnostics, "LAUNCHES_PATH", launches)
        monkeypatch.setattr(claude_diagnostics, "DIAGNOSTICS_PATH", debug_dir / "d.jsonl")
        monkeypatch.setattr(routing, "APP_DIR", tmp_path)

        claude_routing.clear_routing_artifacts()

        assert not debug_dir.exists()
        assert (tmp_path / "debug-logs" / "codex").exists()
        assert (tmp_path / "debug-logs").exists()


class TestStaleLaunchFileSweep:
    def test_removes_dead_pid_files_keeps_live_and_unparsable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(v2, "APP_DIR", tmp_path)
        dead_pid = 999_999_999  # Beyond any allocatable pid: ProcessLookupError.
        dead_json = tmp_path / f"claude-v2-{dead_pid}-deadbeef.json"
        dead_json.write_text("{}", encoding="utf-8")
        dead_sock = tmp_path / f"claude-v2-{dead_pid}-deadbeef.sock"
        dead_sock.write_text("", encoding="utf-8")
        live_json = tmp_path / f"claude-v2-{os.getpid()}-live1234.json"
        live_json.write_text("{}", encoding="utf-8")
        unparsable = tmp_path / "claude-v2-notapid-xyz.json"
        unparsable.write_text("{}", encoding="utf-8")
        other = tmp_path / "claude-v2-model.lock"
        other.write_text("", encoding="utf-8")

        v2._sweep_stale_launch_files()

        assert not dead_json.exists()
        assert not dead_sock.exists()
        assert live_json.exists()
        assert unparsable.exists()
        assert other.exists()
