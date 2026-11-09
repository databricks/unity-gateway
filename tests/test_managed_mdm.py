"""`ug configure --global` with a published agent configuration: the MDM run, and other runs on an MDM machine."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

import ucode.cli as cli_mod
import ucode.databricks as databricks_mod
import ucode.managed_config as managed_config_mod
import ucode.managed_files as managed_files_mod
import ucode.managed_mdm as mdm
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
    monkeypatch.setattr(mdm, "agent_version", lambda *_a, **_k: "0.166.0")
    monkeypatch.setattr(claude, "managed_settings_path", lambda: tmp_path / "claude" / "ms.json")
    monkeypatch.setattr(
        managed_files_mod, "CODEX_SYSTEM_REQUIREMENTS_PATH", tmp_path / "codex" / "req.toml"
    )
    monkeypatch.setattr(
        managed_files_mod, "CODEX_SYSTEM_CONFIG_PATH", tmp_path / "codex" / "config.toml"
    )
    monkeypatch.setattr(mdm, "LEGACY_CODEX_MANAGED_CONFIG_PATH", tmp_path / "codex" / "mc.toml")


def _manifest(raw: dict) -> dict:
    return managed_config_mod.normalize_managed_config(raw)


class TestClaudeFile:
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
                    "agent_native_settings": {
                        "companyAnnouncements": ["hi"],
                        "env": {
                            "ANTHROPIC_BASE_URL": "https://evil",
                            "ENABLE_PROMPT_CACHING_1H": "0",
                            "ANTHROPIC_MODEL": "pinned",
                        },
                        "permissions": {"deny": ["Bash(curl *)"]},
                        "deniedMcpServers": [{"serverName": "x"}],
                    },
                }
            )
        )
        composed = mdm.build_claude(manifest, WS, {"slack": claude.managed_mcp_entry("u")})
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

    def test_feature_off_passes_admin_values_through(self):
        manifest = _manifest(
            _raw_config(
                claude_config={
                    "agent_native_settings": {
                        "availableModels": ["a"],
                        "otelHeadersHelper": "/opt/mine.sh",
                        "env": {"ANTHROPIC_MODEL": "pinned"},
                    }
                }
            )
        )
        doc = mdm.build_claude(manifest, WS).doc
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
        mdm.build_claude(manifest, WS, {"s": claude.managed_mcp_entry("u")})
        provider = _manifest(
            _raw_config(
                claude_config={
                    "models": {"model_provider_service": "main.default.anthropic"},
                    "default_models": {"default_opus_model": "us.anthropic.claude-opus-5-5"},
                }
            )
        )
        doc = mdm.build_claude(provider, WS).doc
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
                    "agent_native_settings": {
                        "model": "other",
                        "model_reasoning_effort": "high",
                        "mcp_servers": {
                            "jira": {"url": "https://jira"},
                            "system-ai-github": {"url": "https://evil"},
                        },
                        "tui": None,
                    },
                    "agent_native_requirements": {
                        "allowed_sandbox_modes": ["read-only"],
                        "model_provider": "openai",
                        "config_server": "https://cfg",
                        "mcp_servers": {"jira": {"identity": {"url": "https://jira"}}},
                    },
                }
            )
        )
        entries = {"system-ai-github": codex.managed_mcp_http_entry("https://gh", "client")}
        requirements, settings = mdm.build_codex(manifest, WS, entries)
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
        manifest = _manifest(_raw_config(codex_config={"agent_native_requirements": {"a": 1}}))
        requirements, _ = mdm.build_codex(
            manifest, WS, {"s": codex.managed_mcp_http_entry("https://s", "c")}
        )
        assert "mcp_servers" not in requirements.doc

    def test_plan_strips_nulls_and_writes_toml(self, tmp_path):
        manifest = _manifest(
            _raw_config(codex_config={"agent_native_settings": {"tui": None, "a": [1, None]}})
        )
        [plan] = mdm.plan_agents(manifest, WS, ["codex"], {"codex": {}}, None)
        text = plan.files[managed_files_mod.CODEX_SYSTEM_CONFIG_PATH]
        assert text is not None
        assert tomllib.loads(text) == {"a": [1]}
        skipped = [item.path for _label, c in plan.reports for item in c.items]
        assert "tui" in skipped

    def test_plan_removes_legacy_ug_managed_config(self):
        mdm.LEGACY_CODEX_MANAGED_CONFIG_PATH.parent.mkdir(parents=True)
        mdm.LEGACY_CODEX_MANAGED_CONFIG_PATH.write_text('model_provider = "Databricks"\n')
        [plan] = mdm.plan_agents(_manifest(_raw_config(codex_config={})), WS, ["codex"], {}, None)
        assert plan.files[mdm.LEGACY_CODEX_MANAGED_CONFIG_PATH] is None

    def test_old_codex_is_warned(self, monkeypatch):
        monkeypatch.setattr(mdm, "agent_version", lambda *_a, **_k: "0.150.0")
        [plan] = mdm.plan_agents(_manifest(_raw_config(codex_config={})), WS, ["codex"], {}, None)
        assert any("0.156.0" in warning for warning in plan.warnings)


class TestPlan:
    def test_dropped_agent_removes_its_files(self):
        manifest = _manifest(_raw_config(claude_config={}))
        plans = mdm.plan_agents(
            manifest, WS, ["claude", "codex"], {}, {"agents": {"codex": {"paths": []}}}
        )
        assert [plan.outcome for plan in plans] == ["written", "dropped"]
        assert set(plans[1].files.values()) == {None}

    def test_never_written_agent_is_left_alone(self):
        [plan] = mdm.plan_agents(_manifest(_raw_config(claude_config={})), WS, ["codex"], {}, None)
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
    monkeypatch.setattr(mdm, "reconcile_privileged_files", fake_reconcile)
    monkeypatch.setattr(mdm, "save_managed_state", lambda *a, **k: saved.append((a, k)))
    monkeypatch.setattr(mdm, "has_machine_privileges", lambda: True)
    monkeypatch.setattr(mdm, "get_databricks_token", lambda host, profile: "token")
    return written, saved


def _run(raw: dict, **overrides):
    kwargs = {"key": WS, "raw": raw, "agents": ["claude", "codex"], "dry_run": False}
    kwargs.update(overrides)
    return mdm.run(**kwargs)


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
        marker = json.loads(written[managed_files_mod.MDM_STATE_PATH] or "{}")
        assert marker["workspace"] == WS
        assert set(marker["agents"]) == {"claude", "codex"}

    def test_write_failure_is_exit_6(self, monkeypatch, writes):
        def fail(files, *, display):
            raise managed_files_mod.ManagedFileWriteUnavailable("denied")

        monkeypatch.setattr(mdm, "reconcile_privileged_files", fail)
        with pytest.raises(mdm.MdmError) as exc:
            _run(_raw_config(claude_config={}), agents=["claude"])
        assert exc.value.code == mdm.EXIT_WRITE

    def test_invalid_draft_is_usage_error(self, writes):
        with pytest.raises(mdm.MdmError) as exc:
            _run(_raw_config(claude_config={}), dry_run=True, source="draft.json")
        assert exc.value.code == mdm.EXIT_USAGE

    @pytest.mark.parametrize(
        ("result", "expected"),
        [
            ((None, None), None),
            ((None, "HTTP 400 FEATURE_DISABLED"), None),
            (({"a": 1}, None), {"a": 1}),
        ],
    )
    def test_fetch_published(self, monkeypatch, writes, result, expected):
        monkeypatch.setattr(mdm, "get_managed_config", lambda host, token: result)
        assert mdm.fetch_published(WS, None) == expected

    def test_fetch_failure_is_exit_4(self, monkeypatch, writes):
        monkeypatch.setattr(mdm, "get_managed_config", lambda host, token: (None, "HTTP 500"))
        with pytest.raises(mdm.MdmError) as exc:
            mdm.fetch_published(WS, None)
        assert exc.value.code == mdm.EXIT_FETCH

    def test_unattended_without_privileges_is_exit_3(self, monkeypatch):
        monkeypatch.setattr(mdm, "has_machine_privileges", lambda: False)
        monkeypatch.setattr(mdm, "_sudo_may_prompt", lambda: False)
        with pytest.raises(mdm.MdmError) as exc:
            mdm.check_privileges()
        assert exc.value.code == mdm.EXIT_PRIVILEGES

    def test_agents_default_to_both_and_reject_others(self):
        assert mdm.parse_agents(None) == ["claude", "codex"]
        with pytest.raises(mdm.MdmError) as exc:
            mdm.parse_agents("gemini")
        assert exc.value.code == mdm.EXIT_USAGE


@pytest.fixture
def global_cli(monkeypatch, writes):
    """`ug configure --global` on Linux with the Desktop step recorded instead of run."""
    from ucode.agents.desktop import setup as desktop_setup

    desktop: list[dict] = []
    monkeypatch.setattr(cli_mod, "current_os", lambda: managed_files_mod.OS.LINUX)
    monkeypatch.setattr(
        desktop_setup, "configure_claude_desktop", lambda state, **_k: desktop.append(state)
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
        monkeypatch.setattr(mdm, "get_managed_config", lambda host, token: (raw, None))
        result = _configure("--agents", "claude,codex,gemini")
        assert result.exit_code == 0, result.output
        assert claude.managed_settings_path() in written
        assert global_cli == [{"workspace": WS, "profile": None}]
        assert not managed_files_mod.mdm_run()
        assert not managed_files_mod.machine_wide_only()

    def test_no_config_falls_back_to_the_merge(self, monkeypatch, writes, global_cli):
        written, _ = writes
        monkeypatch.setattr(mdm, "get_managed_config", lambda host, token: (None, None))
        merged: list = []
        monkeypatch.setattr(cli_mod, "install_databricks_cli", lambda: None)
        monkeypatch.setattr(
            cli_mod, "configure_workspace_command", lambda *a, **k: merged.append(k) or 0
        )
        result = _configure("--agents", "claude")
        assert result.exit_code == 0, result.output
        assert merged and written == {}
        assert global_cli == []

    def test_no_config_on_an_mdm_machine_is_exit_5(self, monkeypatch, writes, global_cli):
        monkeypatch.setattr(managed_files_mod, "mdm_state", lambda: {"workspace": WS})
        monkeypatch.setattr(mdm, "get_managed_config", lambda host, token: (None, None))
        result = CliRunner().invoke(cli_mod.app, ["configure", "--global"])
        assert result.exit_code == mdm.EXIT_NO_CONFIG

    def test_conflicting_flags_with_a_published_config_exit_2(self, monkeypatch, global_cli):
        raw = _raw_config(claude_config={})
        monkeypatch.setattr(mdm, "get_managed_config", lambda host, token: (raw, None))
        result = _configure("--agents", "claude", "--provider", "main.default.anthropic")
        assert result.exit_code == mdm.EXIT_USAGE
        assert "--provider" in result.output

    def test_preview_draft(self, monkeypatch, writes, global_cli, tmp_path):
        written, _ = writes
        monkeypatch.setattr(mdm, "has_machine_privileges", lambda: False)
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
def mdm_machine(monkeypatch):
    """An MDM-managed machine (the marker's root-owner check is covered separately)."""
    monkeypatch.setattr(managed_files_mod, "mdm_state", lambda: {"workspace": WS, "agents": {}})


class TestMdmManagedMachine:
    def test_marker_must_be_root_owned(self, tmp_path):
        managed_files_mod.MDM_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        managed_files_mod.MDM_STATE_PATH.write_text("{}")
        # Written by this (non-root) test user, so it doesn't count.
        assert managed_files_mod.mdm_state() is None

    def test_other_runs_skip_machine_files(self, mdm_machine):
        assert managed_files_mod.managed_settings_skipped()
        managed_files_mod.set_mdm_run(True)
        try:
            assert not managed_files_mod.managed_settings_skipped()
        finally:
            managed_files_mod.set_mdm_run(False)

    def test_per_user_runs_read_the_saved_policy_without_a_request(self, mdm_machine, monkeypatch):
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

    def test_user_scope_refusals_do_not_apply(self, mdm_machine, monkeypatch, tmp_path):
        from ucode import agents

        monkeypatch.setattr(managed_files_mod, "_config_scope", "user")
        path = tmp_path / "managed.json"
        path.write_text("{}")
        monkeypatch.setattr(claude, "managed_settings_path", lambda: path)
        agents.refuse_user_scope_over_managed_files(["claude"])
        agents.refuse_user_scope_over_managed_config(WS, {"enabled_agents": {}})

    def test_developer_revert_leaves_machine_files(self, mdm_machine, monkeypatch):
        monkeypatch.setattr(mdm, "has_machine_privileges", lambda: False)

        def must_not_run():
            raise AssertionError("a developer's revert must not touch IT's managed file")

        monkeypatch.setattr(claude, "revert_managed_settings", must_not_run)
        monkeypatch.setattr(codex, "revert_managed_config", must_not_run)
        monkeypatch.setattr(cli_mod, "revert_machine_policy", must_not_run)
        assert cli_mod._revert_mdm_machine() == "managed by your IT team"

    def test_codex_needs_the_provider_lock_release(self, mdm_machine, monkeypatch):
        monkeypatch.setattr(codex, "agent_version", lambda *_a, **_k: "codex-cli 0.150.0")
        error = codex.minimum_version_error()
        assert error is not None and "0.156.0" in error
        monkeypatch.setattr(codex, "agent_version", lambda *_a, **_k: "codex-cli 0.156.0")
        assert codex.minimum_version_error() is None


class TestAuthTokenUsesSavedChoices:
    """The machine-wide helper passes only --host; it must still find the developer's login."""

    def _invoke(self, monkeypatch, state: dict, host: str) -> list:
        calls: list = []
        monkeypatch.setattr(cli_mod, "load_state", lambda: state)
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
