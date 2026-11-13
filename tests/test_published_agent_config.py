"""Managed runs (`ug configure --global` with a published agent configuration) and the runs after one."""

from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

import ucode.cli as cli_mod
import ucode.databricks as databricks_mod
import ucode.managed_config as managed_config_mod
import ucode.managed_files as managed_files_mod
import ucode.published_agent_config as machine
from ucode import ownership
from ucode.agents import claude, codex

WS = "https://example.cloud.databricks.com"
UG = "/usr/local/bin/ug"


def _raw_config(*, claude_config: dict | None = None, codex_config: dict | None = None) -> dict:
    agents = []
    if claude_config is not None:
        agents.append({"agent": "CODING_AGENT_CLAUDE_CODE", "config": claude_config})
    if codex_config is not None:
        agents.append({"agent": "CODING_AGENT_CODEX", "config": codex_config})
    return {"default_agent": "CODING_AGENT_CLAUDE_CODE", "enabled_agents": agents}


@pytest.fixture(autouse=True)
def _machine(monkeypatch, tmp_path):
    """Fixed binaries and versions, and machine-wide paths under tmp_path."""
    monkeypatch.setattr(databricks_mod, "ug_binary", lambda: UG)
    monkeypatch.setattr(claude, "agent_version", lambda *_a, **_k: "2.1.300")
    monkeypatch.setattr(codex, "agent_version", lambda *_a, **_k: "0.166.0")
    monkeypatch.setattr(machine, "agent_version", lambda *_a, **_k: "0.166.0")
    monkeypatch.setattr(claude, "managed_settings_path", lambda: tmp_path / "claude" / "ms.json")
    monkeypatch.setattr(
        managed_files_mod, "CODEX_SYSTEM_REQUIREMENTS_PATH", tmp_path / "codex" / "req.toml"
    )
    monkeypatch.setattr(
        managed_files_mod, "CODEX_SYSTEM_CONFIG_PATH", tmp_path / "codex" / "config.toml"
    )
    monkeypatch.setattr(machine, "LEGACY_CODEX_MANAGED_CONFIG_PATH", tmp_path / "codex" / "mc.toml")
    monkeypatch.setattr(
        managed_files_mod, "UNITY_GATEWAY_MANAGED_SETTINGS_PATH", tmp_path / "ug" / "ms.json"
    )


def _manifest(raw: dict) -> dict:
    return managed_config_mod.normalize_managed_config(raw)


class TestClaudeFile:
    @pytest.mark.parametrize("admin", [[{"serverName": "linear"}], []], ids=["list", "empty"])
    def test_admin_allowlist_admits_ug_servers(self, admin):
        manifest = _manifest(
            _raw_config(
                claude_config={"claude_code": {"managed_settings": {"allowedMcpServers": admin}}}
            )
        )
        doc = machine.build_claude(manifest, WS, {"uc-github": {}}, {"uc-github", "uc-proxy"}).doc
        assert doc["allowedMcpServers"] == [
            *admin,
            {"serverName": "uc-github"},
            {"serverName": "uc-proxy"},
            {"serverName": claude.WEB_SEARCH_MCP_NAME},
        ]

    def test_no_admin_allowlist_means_ug_adds_none(self):
        doc = machine.build_claude(
            _manifest(_raw_config(claude_config={})), WS, {"uc-github": {}}
        ).doc
        assert "allowedMcpServers" not in doc

    def test_gateway_policy_and_native_settings(self):
        manifest = _manifest(
            _raw_config(
                claude_config={
                    "default_models": {"default_opus_model": "system.ai.claude-opus-5-5"},
                    "models": {
                        "model_services": [
                            "system.ai.claude-sonnet-4-6",
                            "system.ai.claude-sonnet-5-5",
                        ]
                    },
                    "http_headers": {"X-Team": "eng"},
                    "tracing": {"enabled": True},
                    "smart_routing": {"enabled": True},
                    "claude_code": {
                        "managed_settings": {
                            "companyAnnouncements": ["hi"],
                            "env": {
                                "ANTHROPIC_BASE_URL": "https://evil",
                                "ENABLE_PROMPT_CACHING_1H": "0",
                                "ANTHROPIC_MODEL": "pinned",
                            },
                            "permissions": {"deny": ["Bash(curl *)"]},
                            "deniedMcpServers": [{"serverName": "x"}],
                        }
                    },
                }
            )
        )
        composed = machine.build_claude(manifest, WS, {"slack": claude.managed_mcp_entry("u")})
        doc = composed.doc
        assert doc["apiKeyHelper"] == f"{UG} auth-token --host {WS}"
        env = doc["env"]
        assert env["ANTHROPIC_BASE_URL"] == f"{WS}/ai-gateway/anthropic"
        assert "X-Team: eng" in env["ANTHROPIC_CUSTOM_HEADERS"]
        assert env["CLAUDE_CODE_USE_BEDROCK"] == env["CLAUDE_CODE_USE_VERTEX"] == ""
        assert env["ANTHROPIC_AUTH_TOKEN"] == env["ANTHROPIC_API_KEY"] == ""
        assert env["ENABLE_PROMPT_CACHING_1H"] == "0"
        assert env["ENABLE_TOOL_SEARCH"] == "true"
        # The smart-routing model is set per launch, never in the managed file.
        assert "ANTHROPIC_MODEL" not in env
        # Pinned family, and the newest Sonnet in the explicit list for the unpinned one.
        assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "system.ai.claude-opus-5-5[1m]"
        assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "system.ai.claude-sonnet-5-5[1m]"
        assert doc["availableModels"] == [
            "system.ai.claude-sonnet-4-6",
            "system.ai.claude-sonnet-5-5",
        ]
        assert env["OTEL_TRACES_EXPORTER"] == "otlp"
        assert doc["otelHeadersHelper"] == f"{UG} otel-headers --host {WS}"
        assert doc["permissions"]["deny"] == ["Bash(curl *)", "WebSearch"]
        assert doc["managedMcpServers"] == {"slack": claude.managed_mcp_entry("u")}
        assert doc["deniedMcpServers"] == [{"serverName": "x"}]
        assert doc["companyAnnouncements"] == ["hi"]
        assert "--profile" not in json.dumps(doc)
        outcomes = {item.path: item.outcome for item in composed.items}
        assert outcomes["env.ANTHROPIC_BASE_URL"] == ownership.SKIPPED
        assert outcomes["env.ANTHROPIC_MODEL"] == ownership.SKIPPED
        assert outcomes["env.ENABLE_PROMPT_CACHING_1H"] == ownership.OVERRODE_DEFAULT

    def test_both_native_fields_are_written(self):
        manifest = _manifest(
            _raw_config(
                claude_config={
                    "agent_native_settings": {"allowManagedMcpServersOnly": True, "a": 1},
                    "claude_code": {"managed_settings": {"a": 2, "b": 3}},
                }
            )
        )
        doc = machine.build_claude(manifest, WS).doc
        assert doc["allowManagedMcpServersOnly"] is True
        assert (doc["a"], doc["b"]) == (2, 3)

    def test_agent_native_settings_alone_writes_the_same_file(self):
        settings = {"allowManagedMcpServersOnly": True, "permissions": {"deny": ["Bash(rm:*)"]}}

        def build(claude_config: dict) -> dict:
            return machine.build_claude(_manifest(_raw_config(claude_config=claude_config)), WS).doc

        legacy = build({"agent_native_settings": settings})
        assert legacy == build({"claude_code": {"managed_settings": settings}})
        assert legacy["allowManagedMcpServersOnly"] is True
        assert "Bash(rm:*)" in legacy["permissions"]["deny"]

    def test_feature_off_passes_admin_values_through(self):
        manifest = _manifest(
            _raw_config(
                claude_config={
                    "claude_code": {
                        "managed_settings": {
                            "availableModels": ["a"],
                            "otelHeadersHelper": "/opt/mine.sh",
                            "env": {"ANTHROPIC_MODEL": "pinned"},
                        }
                    },
                }
            )
        )
        doc = machine.build_claude(manifest, WS).doc
        assert doc["availableModels"] == ["a"]
        assert doc["otelHeadersHelper"] == "/opt/mine.sh"
        assert doc["env"]["ANTHROPIC_MODEL"] == "pinned"

    def test_every_option_stays_inside_the_ownership_table(self):
        """Coverage: with every UG feature on, compose accepts all of UG's keys."""
        manifest = _manifest(
            _raw_config(
                claude_config={
                    "default_models": {
                        f"default_{family}_model": f"system.ai.claude-{family}-5-5"
                        for family in ("opus", "sonnet", "haiku", "fable")
                    },
                    "models": {"model_services": ["system.ai.claude-opus-5-5"]},
                    "http_headers": {"X-Team": "eng"},
                    "tracing": {"enabled": True},
                    "smart_routing": {"enabled": True},
                }
            )
        )
        machine.build_claude(manifest, WS, {"s": claude.managed_mcp_entry("u")})
        provider = _manifest(
            _raw_config(
                claude_config={
                    "models": {"model_provider_service": "main.default.anthropic"},
                    "default_models": {"default_opus_model": "us.anthropic.claude-opus-5-5"},
                }
            )
        )
        doc = machine.build_claude(provider, WS).doc
        assert (
            "Databricks-Model-Provider-Service: main.default.anthropic"
            in (doc["env"]["ANTHROPIC_CUSTOM_HEADERS"])
        )
        assert "ANTHROPIC_MODEL" not in doc["env"]


class TestCodexFiles:
    def test_requirements_lock_and_settings_defaults(self):
        manifest = _manifest(
            _raw_config(
                codex_config={
                    "default_models": {"default_model": "system.ai.gpt-6-1-sol"},
                    "http_headers": {"X-Team": "eng"},
                    "codex": {
                        "config": {
                            "model": "other",
                            "model_reasoning_effort": "high",
                            "mcp_servers": {
                                "jira": {"url": "https://jira"},
                                "system-ai-github": {"url": "https://evil"},
                            },
                            "tui": None,
                        },
                        "requirements": {
                            "allowed_sandbox_modes": ["read-only"],
                            "model_provider": "openai",
                            "config_server": "https://cfg",
                            "mcp_servers": {"jira": {"identity": {"url": "https://jira"}}},
                        },
                    },
                }
            )
        )
        entries = {"system-ai-github": codex.managed_mcp_http_entry("https://gh", "client")}
        requirements, settings = machine.build_codex(manifest, WS, entries)
        req = requirements.doc
        assert req["model_provider"] == "Databricks"
        provider = req["model_providers"]["Databricks"]
        assert provider["base_url"] == f"{WS}/ai-gateway/codex/v1"
        assert provider["auth"]["command"] == UG
        assert provider["auth"]["args"] == ["auth-token", "--host", WS, "--browser-login"]
        assert provider["http_headers"]["X-Team"] == "eng"
        assert provider["env_http_headers"] == codex._LAUNCH_ROUTING_HEADER_ENV_VARS
        for header in codex._LAUNCH_ROUTING_HEADER_ENV_VARS:
            assert header not in provider["http_headers"]
        assert req["allowed_sandbox_modes"] == ["read-only"]
        assert "config_server" not in req
        assert req["mcp_servers"] == {
            "jira": {"identity": {"url": "https://jira"}},
            "system-ai-github": {"identity": {"url": "https://gh"}},
        }
        assert settings.doc["model"] == "system.ai.gpt-6-1-sol"
        assert settings.doc["model_reasoning_effort"] == "high"
        assert settings.doc["mcp_servers"] == {"jira": {"url": "https://jira"}}
        assert "--profile" not in json.dumps(req)

    def test_no_allowlist_means_ug_adds_none(self):
        manifest = _manifest(_raw_config(codex_config={"codex": {"requirements": {"a": 1}}}))
        requirements, _ = machine.build_codex(
            manifest, WS, {"s": codex.managed_mcp_http_entry("https://s", "c")}
        )
        assert "mcp_servers" not in requirements.doc

    def test_plan_strips_nulls_and_writes_toml(self, tmp_path):
        manifest = _manifest(
            _raw_config(codex_config={"codex": {"config": {"tui": None, "a": [1, None]}}})
        )
        [plan] = machine.plan_agents(manifest, WS, ["codex"], {"codex": {}}, None)
        text = plan.files[managed_files_mod.CODEX_SYSTEM_CONFIG_PATH]
        assert text is not None
        assert tomllib.loads(text) == {"a": [1]}
        skipped = [item.path for _path, c in plan.reports for item in c.items]
        assert "tui" in skipped

    def test_plan_removes_legacy_ug_managed_config(self):
        machine.LEGACY_CODEX_MANAGED_CONFIG_PATH.parent.mkdir(parents=True)
        machine.LEGACY_CODEX_MANAGED_CONFIG_PATH.write_text('model_provider = "Databricks"\n')
        [plan] = machine.plan_agents(
            _manifest(_raw_config(codex_config={})), WS, ["codex"], {}, None
        )
        assert plan.files[machine.LEGACY_CODEX_MANAGED_CONFIG_PATH] is None

    def test_old_codex_is_warned(self, monkeypatch):
        monkeypatch.setattr(machine, "agent_version", lambda *_a, **_k: "0.150.0")
        [plan] = machine.plan_agents(
            _manifest(_raw_config(codex_config={})), WS, ["codex"], {}, None
        )
        assert any("0.156.0" in warning for warning in plan.warnings)


class TestPlan:
    def test_dropped_agent_removes_its_files(self):
        manifest = _manifest(_raw_config(claude_config={}))
        plans = machine.plan_agents(
            manifest, WS, ["claude", "codex"], {}, {"agents": {"codex": {"paths": []}}}
        )
        assert [plan.outcome for plan in plans] == ["written", "dropped"]
        assert set(plans[1].files.values()) == {None}

    def test_never_written_agent_is_left_alone(self):
        [plan] = machine.plan_agents(
            _manifest(_raw_config(claude_config={})), WS, ["codex"], {}, None
        )
        assert plan.outcome == "not enabled"
        assert plan.files == {}


@pytest.fixture
def writes(monkeypatch):
    """Stand-ins for the privileged writer and the policy copy."""
    written: dict[Path, str | None] = {}

    def fake_reconcile(files, *, display):
        for path, text in files.items():
            written[path] = text
            if text is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")

    saved: list[tuple] = []
    monkeypatch.setattr(machine, "reconcile_privileged_files", fake_reconcile)
    monkeypatch.setattr(machine, "save_managed_state", lambda *a, **k: saved.append((a, k)))
    monkeypatch.setattr(machine, "has_machine_privileges", lambda: True)
    monkeypatch.setattr(machine, "get_databricks_token", lambda host, profile: "token")
    return written, saved


def _run(raw: dict, **overrides):
    kwargs = {"key": WS, "raw": raw, "agents": ["claude", "codex"], "dry_run": False}
    kwargs.update(overrides)
    return machine.run(**kwargs)


def _draft(tmp_path) -> Path:
    draft = tmp_path / "draft.json"
    draft.write_text(
        json.dumps(
            {
                "workspace": WS,
                "spec_version": 1,
                **_raw_config(
                    claude_config={"default_models": {"default_model": "system.ai.claude-x"}}
                ),
            }
        )
    )
    return draft


class TestRun:
    def test_writes_files_policy_copy_and_marker(self, writes):
        written, saved = writes
        raw = _raw_config(claude_config={}, codex_config={})
        _run(raw)
        assert claude.managed_settings_path() in written
        assert managed_files_mod.CODEX_SYSTEM_REQUIREMENTS_PATH in written
        assert saved and saved[0][0][1] == raw
        marker = json.loads(written[managed_files_mod.PUBLISHED_AGENT_CONFIG_MARKER_PATH] or "{}")
        assert marker["workspace"] == WS
        assert set(marker["agents"]) == {"claude", "codex"}

    def test_write_failure_is_exit_6(self, monkeypatch, writes):
        def fail(files, *, display):
            raise managed_files_mod.ManagedFileWriteUnavailable("denied")

        monkeypatch.setattr(machine, "reconcile_privileged_files", fail)
        with pytest.raises(machine.ManagedRunError) as exc:
            _run(_raw_config(claude_config={}), agents=["claude"])
        assert exc.value.code == machine.EXIT_WRITE

    def test_failure_partway_restores_every_file(self, monkeypatch, writes, tmp_path):
        written, _ = writes
        claude_path = claude.managed_settings_path()
        claude_path.parent.mkdir(parents=True)
        claude_path.write_text('{"old": true}\n')
        fake_reconcile = machine.reconcile_privileged_files

        requirements = managed_files_mod.CODEX_SYSTEM_REQUIREMENTS_PATH
        requirements.parent.mkdir(parents=True)
        requirements.write_text("old = 1\n")

        def fail_on_codex(files, *, display):
            fake_reconcile(files, display=display)
            if display == "Codex":
                # Codex's files are already replaced when the write reports its failure.
                raise managed_files_mod.ManagedFileWriteUnavailable("denied")

        monkeypatch.setattr(machine, "reconcile_privileged_files", fail_on_codex)
        with pytest.raises(machine.ManagedRunError) as exc:
            _run(_raw_config(claude_config={}, codex_config={}))
        assert exc.value.code == machine.EXIT_WRITE
        assert claude_path.read_text() == '{"old": true}\n'
        assert requirements.read_text() == "old = 1\n"
        assert not managed_files_mod.CODEX_SYSTEM_CONFIG_PATH.exists()
        assert not managed_files_mod.UNITY_GATEWAY_MANAGED_SETTINGS_PATH.exists()
        assert not managed_files_mod.PUBLISHED_AGENT_CONFIG_MARKER_PATH.exists()

    @pytest.mark.parametrize(
        "failure",
        [
            subprocess.CalledProcessError(1, ["sudo"]),
            RuntimeError("read back failed"),
            KeyboardInterrupt(),
        ],
    )
    def test_any_failure_on_the_marker_restores_every_file(self, monkeypatch, writes, failure):
        claude_path = claude.managed_settings_path()
        claude_path.parent.mkdir(parents=True)
        claude_path.write_text('{"old": true}\n')
        fake_reconcile = machine.reconcile_privileged_files

        failed: list[bool] = []

        def fail_on_marker(files, *, display):
            # Fail the marker write once; the restore that follows writes the marker path too.
            if managed_files_mod.PUBLISHED_AGENT_CONFIG_MARKER_PATH in files and not failed:
                failed.append(True)
                raise failure
            fake_reconcile(files, display=display)

        monkeypatch.setattr(machine, "reconcile_privileged_files", fail_on_marker)
        expected = (
            KeyboardInterrupt if isinstance(failure, KeyboardInterrupt) else machine.ManagedRunError
        )
        with pytest.raises(expected) as exc:
            _run(_raw_config(claude_config={}), agents=["claude"])
        if expected is machine.ManagedRunError:
            assert exc.value.code == machine.EXIT_WRITE
        assert claude_path.read_text() == '{"old": true}\n'

    def test_a_new_workspace_rebuilds_every_agent(self, monkeypatch, writes, capsys):
        written, _ = writes
        old = {
            "workspace": "https://other.cloud.databricks.com",
            "agents": {"codex": {"paths": []}},
        }
        monkeypatch.setattr(machine, "published_agent_config_marker", lambda: old)
        _run(_raw_config(claude_config={}, codex_config={}), agents=["claude"])
        assert managed_files_mod.CODEX_SYSTEM_REQUIREMENTS_PATH in written
        assert "every agent is rebuilt" in capsys.readouterr().out

    def test_the_same_workspace_rebuilds_only_the_requested_agents(self, monkeypatch, writes):
        written, _ = writes
        same = {"workspace": WS, "agents": {"codex": {"paths": []}}}
        monkeypatch.setattr(machine, "published_agent_config_marker", lambda: same)
        _run(_raw_config(claude_config={}, codex_config={}), agents=["claude"])
        assert managed_files_mod.CODEX_SYSTEM_REQUIREMENTS_PATH not in written

    def test_another_writer_keeps_its_file_and_the_rest_are_restored(self, monkeypatch, writes):
        claude_path = claude.managed_settings_path()
        claude_path.parent.mkdir(parents=True)
        claude_path.write_text('{"old": true}\n')
        requirements = managed_files_mod.CODEX_SYSTEM_REQUIREMENTS_PATH
        fake_reconcile = machine.reconcile_privileged_files

        def other_writer(files, *, display):
            fake_reconcile(files, display=display)
            if display == "Codex":
                requirements.write_text("# theirs\n")

        monkeypatch.setattr(machine, "reconcile_privileged_files", other_writer)
        with pytest.raises(machine.FileChangedDuringWrite) as exc:
            _run(_raw_config(claude_config={}, codex_config={}))
        assert exc.value.path == requirements
        assert requirements.read_text() == "# theirs\n"
        assert claude_path.read_text() == '{"old": true}\n'
        assert not managed_files_mod.CODEX_SYSTEM_CONFIG_PATH.exists()
        assert not managed_files_mod.PUBLISHED_AGENT_CONFIG_MARKER_PATH.exists()

    def test_dry_run_lists_each_file_action(self, writes, capsys):
        written, _ = writes
        req = managed_files_mod.CODEX_SYSTEM_REQUIREMENTS_PATH
        req.parent.mkdir(parents=True)
        req.write_text("old = 1\n")
        _run(_raw_config(claude_config={}, codex_config={}), dry_run=True)
        out = capsys.readouterr().out
        assert written == {}
        assert f"replace  {req}" in out
        assert f"create   {claude.managed_settings_path()}" in out
        assert f"create   {managed_files_mod.PUBLISHED_AGENT_CONFIG_MARKER_PATH}" in out
        assert f"create   {managed_files_mod.UNITY_GATEWAY_MANAGED_SETTINGS_PATH}" in out

    def test_dry_run_lists_remove_and_unchanged(self, monkeypatch, writes, capsys):
        raw = _raw_config(claude_config={}, codex_config={})
        _run(raw)
        written_marker = json.loads(
            managed_files_mod.PUBLISHED_AGENT_CONFIG_MARKER_PATH.read_text()
        )
        monkeypatch.setattr(machine, "published_agent_config_marker", lambda: written_marker)
        capsys.readouterr()
        # Codex is no longer enabled; Claude's file would come out the same.
        _run(_raw_config(claude_config={}), dry_run=True)
        actions = [line.split() for line in capsys.readouterr().out.splitlines()]
        assert ["unchanged", str(claude.managed_settings_path())] in actions
        assert ["remove", str(managed_files_mod.CODEX_SYSTEM_REQUIREMENTS_PATH)] in actions
        assert ["remove", str(managed_files_mod.CODEX_SYSTEM_CONFIG_PATH)] in actions

    def test_report_lists_each_admin_key_under_its_file(self, writes, capsys):
        raw = _raw_config(
            claude_config={"claude_code": {"managed_settings": {"apiKeyHelper": "mine", "a": 1}}},
            codex_config={"codex": {"config": {"model": "gpt-5"}}},
        )
        _run(raw, dry_run=True)
        lines = capsys.readouterr().out.splitlines()
        start = lines.index("Claude Code: would write")
        assert lines[start + 1 : start + 4] == [
            f"  {claude.managed_settings_path()}",
            "    [skipped] apiKeyHelper — Runs `ug auth-token`, which signs in as the developer.",
            "    [applied] a",
        ]
        codex_start = lines.index("Codex: would write")
        assert lines[codex_start + 1 : codex_start + 3] == [
            f"  {managed_files_mod.CODEX_SYSTEM_CONFIG_PATH}",
            "    [skipped] model — The default model comes from `default_models.default_model`.",
        ]

    def test_invalid_draft_is_usage_error(self, writes):
        with pytest.raises(machine.ManagedRunError) as exc:
            _run(_raw_config(claude_config={}), dry_run=True, source="draft.json")
        assert exc.value.code == machine.EXIT_USAGE

    @pytest.mark.parametrize(
        ("result", "expected"),
        [
            ((None, None), None),
            ((None, "HTTP 400 FEATURE_DISABLED"), None),
            (({"a": 1}, None), {"a": 1}),
        ],
    )
    def test_fetch_published(self, monkeypatch, writes, result, expected):
        monkeypatch.setattr(machine, "get_managed_config", lambda host, token: result)
        assert machine.fetch_published(WS, "token") == expected

    def test_fetch_failure_is_exit_4(self, monkeypatch, writes):
        monkeypatch.setattr(machine, "get_managed_config", lambda host, token: (None, "HTTP 500"))
        with pytest.raises(machine.ManagedRunError) as exc:
            machine.fetch_published(WS, "token")
        assert exc.value.code == machine.EXIT_FETCH

    def test_unattended_without_privileges_is_exit_3(self, monkeypatch):
        monkeypatch.setattr(machine, "has_machine_privileges", lambda: False)
        monkeypatch.setattr(machine, "_sudo_may_prompt", lambda: False)
        with pytest.raises(machine.ManagedRunError) as exc:
            machine.check_privileges()
        assert exc.value.code == machine.EXIT_PRIVILEGES

    def test_agents_default_to_both_and_reject_others(self):
        assert machine.parse_agents(None) == ["claude", "codex"]
        with pytest.raises(machine.ManagedRunError) as exc:
            machine.parse_agents("gemini")
        assert exc.value.code == machine.EXIT_USAGE


@pytest.fixture
def global_cli(monkeypatch, writes):
    """`ug configure --global` on Linux with the Desktop step recorded instead of run."""
    from ucode.agents.desktop import setup as desktop_setup

    desktop: list[dict] = []
    monkeypatch.setattr(cli_mod, "current_os", lambda: managed_files_mod.OS.LINUX)
    monkeypatch.setattr(cli_mod, "install_databricks_cli", lambda: None)
    monkeypatch.setattr(
        desktop_setup, "configure_claude_desktop", lambda state, **_k: desktop.append(state)
    )
    monkeypatch.setattr(
        desktop_setup, "revert_claude_desktop_mdm", lambda: desktop.append("removed") or "removed"
    )
    return desktop


def _configure(*args: str):
    return CliRunner().invoke(cli_mod.app, ["configure", "--global", "--workspace", WS, *args])


class TestGlobalCli:
    def test_published_config_rebuilds_and_provisions_desktop(
        self, monkeypatch, writes, global_cli
    ):
        written, _ = writes
        raw = _raw_config(claude_config={}, codex_config={})
        monkeypatch.setattr(machine, "get_managed_config", lambda host, token: (raw, None))
        result = _configure("--agents", "claude,codex,gemini")
        assert result.exit_code == 0, result.output
        assert claude.managed_settings_path() in written
        assert global_cli == [{"workspace": WS, "profile": None}]
        assert not managed_files_mod.machine_wide_only()

    def test_no_cached_sign_in_signs_in_before_deciding(self, monkeypatch, writes, global_cli):
        written, _ = writes
        raw = _raw_config(claude_config={})
        signed_in: list = []

        def token(host, profile):
            if not signed_in:
                raise RuntimeError("no cached credential")
            return "token"

        monkeypatch.setattr(machine, "get_databricks_token", token)
        monkeypatch.setattr(
            cli_mod, "ensure_databricks_auth", lambda host, profile, **k: signed_in.append(host)
        )
        monkeypatch.setattr(machine, "get_managed_config", lambda host, token: (raw, None))
        result = _configure("--agents", "claude")
        assert result.exit_code == 0, result.output
        assert signed_in == [WS]
        assert claude.managed_settings_path() in written

    def test_mcp_servers_are_listed_as_the_run_signed_in(self, monkeypatch, writes, global_cli):
        from ucode import mcp

        raw = _raw_config(claude_config={})
        monkeypatch.setattr(machine, "get_managed_config", lambda host, token: (raw, None))
        seen: list = []

        def resolve(selector, workspace, profile, clients, *, token=None):
            seen.append((profile, token))
            return []

        monkeypatch.setattr(mcp, "_resolve_managed_mcp_servers", resolve)
        manifest = {"mcp_servers": {"unity_catalog_location": "main.tools"}}
        monkeypatch.setattr(
            machine, "normalize_managed_config", lambda raw: {**_manifest(raw), **manifest}
        )
        result = _configure("--agents", "claude", "--profile", "ADMIN")
        assert result.exit_code == 0, result.output
        assert seen == [("ADMIN", "token")]

    @pytest.mark.parametrize("dry_run", [False, True])
    def test_dropping_claude_removes_desktop_mdm(self, monkeypatch, writes, global_cli, dry_run):
        raw = _raw_config(codex_config={})
        monkeypatch.setattr(machine, "get_managed_config", lambda host, token: (raw, None))
        result = _configure("--agents", "claude", *(["--dry-run"] if dry_run else []))
        assert result.exit_code == 0, result.output
        assert global_cli == ([] if dry_run else ["removed"])

    def test_the_databricks_cli_is_installed_before_sign_in(self, monkeypatch, writes, global_cli):
        order: list[str] = []
        raw = _raw_config(claude_config={})
        monkeypatch.setattr(cli_mod, "install_databricks_cli", lambda: order.append("install"))

        def token(host, profile):
            order.append("sign in")
            return "token"

        monkeypatch.setattr(machine, "get_databricks_token", token)
        monkeypatch.setattr(machine, "get_managed_config", lambda host, token: (raw, None))
        result = _configure("--agents", "claude")
        assert result.exit_code == 0, result.output
        assert order[:2] == ["install", "sign in"]

    def test_a_missing_databricks_cli_is_a_sign_in_failure(self, monkeypatch, writes, global_cli):
        def missing(host, profile):
            raise FileNotFoundError("databricks")

        monkeypatch.setattr(machine, "get_databricks_token", missing)
        monkeypatch.setattr(
            managed_files_mod, "published_agent_config_marker", lambda: {"workspace": WS}
        )
        result = _configure("--agents", "claude")
        assert result.exit_code == machine.EXIT_FETCH, result.output

    def test_a_saved_user_scope_does_not_block_global(self, monkeypatch, writes, global_cli):
        written, _ = writes
        raw = _raw_config(claude_config={})
        monkeypatch.setattr(machine, "get_managed_config", lambda host, token: (raw, None))
        managed_files_mod.set_config_scope("user")
        result = _configure("--agents", "claude")
        assert result.exit_code == 0, result.output
        assert claude.managed_settings_path() in written
        assert managed_files_mod.config_scope() == "global"

    def test_skip_os_settings_is_rejected_before_any_write(self, monkeypatch, writes, global_cli):
        written, _ = writes
        monkeypatch.setenv(managed_files_mod.SKIP_OS_SETTINGS_ENV, "1")

        def no_request(*_a):
            raise AssertionError("the opt-out must be rejected before the configuration is read")

        monkeypatch.setattr(machine, "get_managed_config", no_request)
        result = _configure("--agents", "claude")
        assert result.exit_code != 0
        assert managed_files_mod.SKIP_OS_SETTINGS_ENV in result.output
        assert written == {}

    @pytest.mark.parametrize("agent", ["claude", "codex"])
    def test_a_relayed_provider_service_is_rejected_before_any_write(
        self, monkeypatch, writes, global_cli, agent
    ):
        written, _ = writes
        config = {"models": {"model_provider_service": "main.relay.sub"}}
        raw = _raw_config(**{f"{agent}_config": config})
        monkeypatch.setattr(machine, "get_managed_config", lambda host, token: (raw, None))
        monkeypatch.setattr(
            machine,
            "get_model_provider_service",
            lambda name, host, token: ({"name": name, "relayed": True}, None),
        )
        result = _configure("--agents", agent)
        assert result.exit_code == machine.EXIT_USAGE, result.output
        assert "relays a subscription" in " ".join(result.output.split())
        assert written == {}

    def test_a_failed_read_before_the_first_managed_run_merges(
        self, monkeypatch, writes, global_cli
    ):
        written, _ = writes
        monkeypatch.setattr(machine, "get_managed_config", lambda host, token: (None, "HTTP 503"))
        merged: list = []
        monkeypatch.setattr(
            cli_mod, "configure_workspace_command", lambda *a, **k: merged.append(k) or 0
        )
        result = _configure("--agents", "claude")
        assert result.exit_code == 0, result.output
        assert "HTTP 503. Merging into the machine-wide files instead." in " ".join(
            result.output.split()
        )
        assert merged and written == {}

    def test_a_failed_read_after_a_managed_run_is_exit_4(self, monkeypatch, writes, global_cli):
        monkeypatch.setattr(
            managed_files_mod, "published_agent_config_marker", lambda: {"workspace": WS}
        )
        monkeypatch.setattr(machine, "get_managed_config", lambda host, token: (None, "HTTP 503"))
        result = _configure("--agents", "claude")
        assert result.exit_code == machine.EXIT_FETCH

    def test_no_config_falls_back_to_the_merge(self, monkeypatch, writes, global_cli):
        written, _ = writes
        monkeypatch.setattr(machine, "get_managed_config", lambda host, token: (None, None))
        merged: list = []
        monkeypatch.setattr(cli_mod, "install_databricks_cli", lambda: None)
        monkeypatch.setattr(
            cli_mod, "configure_workspace_command", lambda *a, **k: merged.append(k) or 0
        )
        result = _configure("--agents", "claude")
        assert result.exit_code == 0, result.output
        assert merged and written == {}
        assert global_cli == []

    def test_no_config_after_a_published_agent_config_is_exit_5(
        self, monkeypatch, writes, global_cli
    ):
        monkeypatch.setattr(
            managed_files_mod, "published_agent_config_marker", lambda: {"workspace": WS}
        )
        monkeypatch.setattr(machine, "get_managed_config", lambda host, token: (None, None))
        result = CliRunner().invoke(cli_mod.app, ["configure", "--global"])
        assert result.exit_code == machine.EXIT_NO_CONFIG

    def test_conflicting_flags_with_a_published_config_exit_2(self, monkeypatch, global_cli):
        raw = _raw_config(claude_config={})
        monkeypatch.setattr(machine, "get_managed_config", lambda host, token: (raw, None))
        result = _configure("--agents", "claude", "--provider", "main.default.anthropic")
        assert result.exit_code == machine.EXIT_USAGE
        assert "--provider" in result.output

    def test_preview_draft(self, monkeypatch, writes, global_cli, tmp_path):
        written, _ = writes
        monkeypatch.setattr(machine, "has_machine_privileges", lambda: False)
        result = _configure("--dry-run", "--agents", "claude", "--from-file", str(_draft(tmp_path)))
        assert result.exit_code == 0, result.output
        assert "apiKeyHelper" in result.output
        assert written == {} and global_cli == []

    def test_from_file_requires_global_dry_run(self, tmp_path):
        result = CliRunner().invoke(
            cli_mod.app, ["configure", "--from-file", str(tmp_path / "x.json")]
        )
        assert result.exit_code == 2
        result = _configure("--from-file", str(tmp_path / "x.json"))
        assert result.exit_code == 2


@pytest.fixture
def published_agent_config_used_fixture(monkeypatch):
    """A machine that used a published agent configuration (the marker's root-owner check is covered separately)."""
    monkeypatch.setattr(
        managed_files_mod, "published_agent_config_marker", lambda: {"workspace": WS, "agents": {}}
    )


class TestAfterPublishedAgentConfig:
    def test_marker_must_be_root_owned(self, tmp_path):
        managed_files_mod.PUBLISHED_AGENT_CONFIG_MARKER_PATH.parent.mkdir(
            parents=True, exist_ok=True
        )
        managed_files_mod.PUBLISHED_AGENT_CONFIG_MARKER_PATH.write_text("{}")
        # Written by this (non-root) test user, so it doesn't count.
        assert managed_files_mod.published_agent_config_marker() is None

    def test_only_an_explicit_global_writes_machine_files(
        self, published_agent_config_used_fixture, monkeypatch
    ):
        assert managed_files_mod.managed_settings_skipped()
        monkeypatch.setattr(managed_files_mod, "_machine_wide_only", True)
        assert not managed_files_mod.managed_settings_skipped()
        monkeypatch.setenv(managed_files_mod.SKIP_OS_SETTINGS_ENV, "1")
        assert managed_files_mod.managed_settings_skipped()

    def test_per_user_runs_read_the_saved_policy_without_a_request(
        self, published_agent_config_used_fixture, monkeypatch
    ):
        raw = _raw_config(claude_config={"http_headers": {"X": "1"}})
        managed_config_mod.MANAGED_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        managed_config_mod.MANAGED_CONFIG_PATH.write_text(
            json.dumps({"workspace": WS, "config": raw})
        )

        def no_request(*_args, **_kwargs):
            raise AssertionError("per-user runs on an MDM machine must not fetch the policy")

        monkeypatch.setattr(managed_config_mod, "get_managed_config", no_request)
        result = managed_config_mod.refresh_managed_config({"workspace": WS}, force_refresh=True)
        assert result.manifest == managed_config_mod.normalize_managed_config(raw)

    def test_another_workspace_warns_that_its_configuration_is_not_applied(
        self, monkeypatch, capsys
    ):
        monkeypatch.setattr(
            managed_files_mod, "published_agent_config_marker", lambda: {"workspace": WS}
        )
        monkeypatch.setattr(
            managed_config_mod, "published_agent_config_marker", lambda: {"workspace": WS}
        )
        other = "https://other.cloud.databricks.com"
        result = managed_config_mod.refresh_managed_config({"workspace": other}, force_refresh=True)
        assert result.manifest is None
        assert f"{other}'s agent configuration isn't applied" in " ".join(
            capsys.readouterr().out.split()
        )

    def test_user_scope_refusals_do_not_apply(
        self, published_agent_config_used_fixture, monkeypatch, tmp_path
    ):
        from ucode import agents

        monkeypatch.setattr(managed_files_mod, "_config_scope", "user")
        path = tmp_path / "managed.json"
        path.write_text("{}")
        monkeypatch.setattr(claude, "managed_settings_path", lambda: path)
        agents.refuse_user_scope_over_managed_files(["claude"])
        agents.refuse_user_scope_over_managed_config(WS, {"enabled_agents": {}})

    def test_root_revert_removes_everything_then_runs_the_ordinary_restores(
        self, monkeypatch, writes, tmp_path
    ):
        from ucode.agents.desktop import setup as desktop_setup

        written, _ = writes
        marker = managed_files_mod.PUBLISHED_AGENT_CONFIG_MARKER_PATH
        saved = managed_files_mod.UNITY_GATEWAY_MANAGED_SETTINGS_PATH
        claude_path = claude.managed_settings_path()
        codex_paths = [
            managed_files_mod.CODEX_SYSTEM_REQUIREMENTS_PATH,
            managed_files_mod.CODEX_SYSTEM_CONFIG_PATH,
        ]
        for path in (marker, saved, claude_path, *codex_paths):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n")
        state = {
            "workspace": WS,
            "agents": {
                "claude": {"paths": [str(claude_path)]},
                "codex": {"paths": [str(path) for path in codex_paths]},
            },
        }
        for module in (managed_files_mod, machine):
            monkeypatch.setattr(
                module, "published_agent_config_marker", lambda: state if marker.exists() else None
            )

        restores: list[str] = []

        def restore(name):
            def run():
                # They clean up an earlier merge only once the managed run's files are gone.
                assert not marker.exists()
                restores.append(name)
                return "unchanged"

            return run

        monkeypatch.setattr(cli_mod, "_running_as_root", lambda: True)
        monkeypatch.setattr(claude, "revert_managed_settings", restore("claude"))
        monkeypatch.setattr(codex, "revert_managed_config", restore("codex"))
        monkeypatch.setattr(cli_mod, "revert_machine_policy", restore("policy"))
        monkeypatch.setattr(desktop_setup, "revert_desktop", lambda: "unchanged")
        monkeypatch.setattr(cli_mod, "revert_mcp_configs", lambda state: {})
        monkeypatch.setattr(cli_mod, "restore_file", lambda *args: False)
        monkeypatch.setattr(cli_mod, "revert_legacy_shared_config", lambda: False)
        monkeypatch.setattr(cli_mod.copilot_agent, "scrub_stored_credentials", lambda: None)
        monkeypatch.setattr(cli_mod.copilot_agent, "sweep_session_registries", lambda: None)

        result = CliRunner().invoke(cli_mod.app, ["revert"], env={"COLUMNS": "240"})

        assert result.exit_code == 0, result.output
        assert not any(path.exists() for path in (claude_path, *codex_paths, saved, marker))
        assert list(written) == [claude_path, *codex_paths, saved, marker]
        assert restores == ["claude", "codex", "policy"]
        assert "no UG backup record" not in result.output

    def test_developer_revert_leaves_machine_files(
        self, published_agent_config_used_fixture, monkeypatch
    ):
        # Passwordless or cached sudo doesn't make a developer's revert remove them.
        monkeypatch.setattr(machine, "has_machine_privileges", lambda: True)
        monkeypatch.setattr(cli_mod, "_running_as_root", lambda: False)

        def no_removal():
            raise AssertionError("a developer's revert must not remove the managed run's files")

        monkeypatch.setattr(machine, "revert_machine", no_removal)

        def must_not_run():
            raise AssertionError("a developer's revert must not touch IT's managed file")

        monkeypatch.setattr(claude, "revert_managed_settings", must_not_run)
        monkeypatch.setattr(codex, "revert_managed_config", must_not_run)
        monkeypatch.setattr(cli_mod, "revert_machine_policy", must_not_run)
        assert cli_mod._revert_published_agent_config() == "managed, left as is"


class TestAuthTokenUsesSavedChoices:
    """The machine-wide helper passes only --host; it must still find the developer's login."""

    def _invoke(self, monkeypatch, state: dict, host: str, *, marker: bool = True) -> list:
        calls: list = []
        monkeypatch.setattr(cli_mod, "load_state", lambda: state)
        monkeypatch.setattr(cli_mod, "published_agent_config_used", lambda: marker)
        monkeypatch.setattr(cli_mod, "ensure_pat_bearer", lambda profile: True)
        monkeypatch.setattr(
            cli_mod,
            "get_databricks_token",
            lambda workspace, profile, **_k: calls.append(("token", profile)) or "t",
        )
        monkeypatch.setattr(
            cli_mod.custom_oauth,
            "get_custom_client_token",
            lambda workspace, **k: calls.append(("custom", k["client_id"], k["profile"])) or "t",
        )
        result = CliRunner().invoke(cli_mod.app, ["auth-token", "--host", host])
        assert result.exit_code == 0, result.output
        return calls

    def test_matching_host_uses_saved_profile(self, monkeypatch):
        calls = self._invoke(monkeypatch, {"workspace": WS, "profile": "mine"}, WS + "/")
        assert calls == [("token", "mine")]

    def test_without_a_managed_run_the_helper_keeps_its_own_choices(self, monkeypatch):
        # A per-user helper names its choices, so a later configure's saved ones don't apply.
        calls = self._invoke(monkeypatch, {"workspace": WS, "profile": "mine"}, WS, marker=False)
        assert calls == [("token", None)]

    def test_other_host_does_not(self, monkeypatch):
        calls = self._invoke(
            monkeypatch, {"workspace": WS, "profile": "mine"}, "https://other.databricks.com"
        )
        assert calls == [("token", None)]

    def test_matching_host_uses_saved_custom_oauth(self, monkeypatch):
        state = {
            "workspace": WS,
            "profile": "plain",
            "custom_oauth": {
                "client_id": "cid",
                "redirect_url": "http://localhost:8020",
                "scopes": ["all-apis"],
                "profile": "oauth-profile",
            },
        }
        calls = self._invoke(monkeypatch, state, WS)
        assert calls == [("custom", "cid", "oauth-profile")]
