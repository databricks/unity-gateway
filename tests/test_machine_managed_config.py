"""`ug configure` publishing the managed config machine-wide (/etc/unitygateway/managed-config.json)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

import ucode.cli as cli_mod
import ucode.config_io as config_io
import ucode.managed_config as mc
import ucode.managed_files as mf

WS = "https://example.databricks.com"
OTHER_WS = "https://other.databricks.com"

# A raw CodingAgentConfig as the gateway returns it (proto enum agent names, wire keys).
RAW = {
    "name": "coding-agents-default",
    "update_time": "2026-10-20T10:00:00Z",
    "enabled_agents": [
        {
            "agent": "CODING_AGENT_CLAUDE_CODE",
            "config": {
                "models": {"model_provider_service": "main.default.anthropic"},
                "http_headers": {"X-Team": "platform"},
                "agent_native_settings": {"env": {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "32000"}},
            },
        },
        {
            "agent": "CODING_AGENT_CODEX",
            "config": {"models": {"unity_catalog_location": "main.models"}},
        },
    ],
}


@pytest.fixture
def machine_file(monkeypatch):
    """Capture the privileged write: the fake applies the text to the (tmp) machine path."""
    path = mc.MACHINE_MANAGED_CONFIG_PATH
    writes: list[str] = []

    def write(target, text, **_kwargs):
        assert target == path
        current = target.read_text(encoding="utf-8") if target.exists() else None
        if current == text:
            return "unchanged"
        writes.append(text)
        target.write_text(text, encoding="utf-8")
        return "written" if current is not None else "created"

    monkeypatch.setattr(mc, "write_machine_file", write)
    return path, writes


def _authoritative(raw: dict | None, *, workspace: str = WS, outcome: str = "published") -> dict:
    """Record an authoritative read exactly as `refresh_managed_config` does."""
    mc.save_managed_state(workspace, raw or {}, outcome=outcome)
    return mc.normalize_managed_config(raw) if raw else {}


def _published(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class TestSyncMachineManagedConfig:
    def test_publishes_raw_config_with_derived_ug_state(self, machine_file):
        path, _writes = machine_file
        managed = _authoritative(RAW)

        assert mc.sync_machine_managed_config(WS, managed, ["claude", "codex"]) == "created"

        payload = _published(path)
        assert set(payload) == {"workspace", "config", "retrieved_at", "outcome", "ug_state"}
        assert payload["workspace"] == WS
        assert payload["outcome"] == "published"
        # The raw CodingAgentConfig is kept verbatim, not normalized.
        assert payload["config"] == RAW
        read = mc.authoritative_managed_read(WS)
        assert read is not None
        assert payload["retrieved_at"] == read["retrieved_at"]
        assert payload["ug_state"] == {
            "version": 1,
            "available_tools": ["claude", "codex"],
            "provider_services": {"claude": "main.default.anthropic"},
            "model_locations": {"codex": "main.models"},
        }

    def test_failed_agent_is_not_published(self, machine_file):
        path, _writes = machine_file
        managed = _authoritative(RAW)

        mc.sync_machine_managed_config(WS, managed, ["codex"])

        ug_state = _published(path)["ug_state"]
        assert ug_state["available_tools"] == ["codex"]
        assert ug_state["provider_services"] == {}
        assert ug_state["model_locations"] == {"codex": "main.models"}

    def test_identical_configure_is_a_no_op(self, machine_file, monkeypatch):
        path, writes = machine_file
        managed = _authoritative(RAW)
        mc.sync_machine_managed_config(WS, managed, ["claude", "codex"])
        first = path.read_text(encoding="utf-8")

        # A later fetch of the same policy carries a new read time only.
        monkeypatch.setattr(mc, "_utcnow", lambda: mc.datetime(2030, 1, 1, tzinfo=mc.UTC))
        managed = _authoritative(RAW)
        assert mc.sync_machine_managed_config(WS, managed, ["claude", "codex"]) == "unchanged"

        assert len(writes) == 1
        assert path.read_text(encoding="utf-8") == first

    def test_without_a_published_policy_no_file_is_created(self, machine_file):
        path, writes = machine_file
        _authoritative(None, outcome="none")

        assert mc.sync_machine_managed_config(WS, None, []) == "unchanged"
        assert writes == []
        assert not path.exists()

    def test_failed_fetch_neither_publishes_nor_invalidates(self, machine_file):
        path, writes = machine_file
        managed = _authoritative(RAW)
        mc.sync_machine_managed_config(WS, managed, ["claude"])
        writes.clear()
        # A later refresh whose fetch fails falls back to the cache: no authoritative read.
        mc._authoritative_reads.clear()

        assert mc.sync_machine_managed_config(WS, managed, ["claude", "codex"]) == "skipped"
        assert mc.sync_machine_managed_config(WS, None, []) == "skipped"
        assert writes == []
        assert _published(path)["outcome"] == "published"

    @pytest.mark.parametrize("outcome", ["none", "feature_disabled"])
    def test_removed_policy_invalidates_the_published_copy(self, machine_file, outcome):
        path, _writes = machine_file
        mc.sync_machine_managed_config(WS, _authoritative(RAW), ["claude"])

        _authoritative(None, outcome=outcome)
        assert mc.sync_machine_managed_config(WS, None, []) == "invalidated"

        payload = _published(path)
        assert payload["outcome"] == outcome
        assert payload["config"] == {}
        assert "ug_state" not in payload

    def test_switching_to_an_unmanaged_workspace_invalidates(self, machine_file):
        path, _writes = machine_file
        mc.sync_machine_managed_config(WS, _authoritative(RAW), ["claude"])

        _authoritative(None, workspace=OTHER_WS, outcome="none")
        assert mc.sync_machine_managed_config(OTHER_WS, None, []) == "invalidated"

        payload = _published(path)
        assert payload["workspace"] == OTHER_WS
        assert payload["outcome"] == "none"

    def test_switching_to_another_managed_workspace_replaces_it(self, machine_file):
        path, _writes = machine_file
        mc.sync_machine_managed_config(WS, _authoritative(RAW), ["claude", "codex"])

        other = {**RAW, "name": "other"}
        managed = _authoritative(other, workspace=OTHER_WS)
        mc.sync_machine_managed_config(OTHER_WS, managed, ["codex"], keep_configured=True)

        payload = _published(path)
        assert payload["workspace"] == OTHER_WS
        assert payload["config"] == other
        # Agents published for the old workspace are not carried over.
        assert payload["ug_state"]["available_tools"] == ["codex"]

    def test_one_agent_configure_keeps_earlier_agents_for_the_same_policy(self, machine_file):
        path, _writes = machine_file
        mc.sync_machine_managed_config(WS, _authoritative(RAW), ["claude", "codex"])

        managed = _authoritative(RAW)
        mc.sync_machine_managed_config(WS, managed, ["claude"], keep_configured=True)

        assert _published(path)["ug_state"]["available_tools"] == ["claude", "codex"]

    @pytest.mark.parametrize(
        ("agent_config", "field"),
        [
            ({"http_headers": {"Authorization": "x"}}, "http_headers.Authorization"),
            ({"http_headers": {"X-Team": "Bearer abc123"}}, "http_headers.X-Team"),
            (
                {"agent_native_settings": {"env": {"GITHUB_TOKEN": "ghp_x"}}},
                "agent_native_settings.env.GITHUB_TOKEN",
            ),
            (
                {
                    "agent_native_settings": {
                        "env": {
                            "ANTHROPIC_CUSTOM_HEADERS": "X-Tag: a\nAuthorization: Bearer abcdefgh"
                        }
                    }
                },
                "agent_native_settings.env.ANTHROPIC_CUSTOM_HEADERS",
            ),
            (
                {
                    "agent_native_settings": {
                        "env": {"OTEL_EXPORTER_OTLP_HEADERS": "x=y,a=Bearer abcd"}
                    }
                },
                "agent_native_settings.env.OTEL_EXPORTER_OTLP_HEADERS",
            ),
            (
                {"http_headers": {"X-Proxy": "https://user:pw@proxy.example"}},
                "http_headers.X-Proxy",
            ),
            ({"http_headers": {"X-Feed": "https://h.example/?api_key=abc"}}, "http_headers.X-Feed"),
            ({"http_headers": {"X-Sig": "sk-abcdefghijkl"}}, "http_headers.X-Sig"),
            ({"agent_native_settings": {"env": {"GITHUB_PAT": "x"}}}, "env.GITHUB_PAT"),
        ],
    )
    def test_possible_credentials_are_never_published(self, machine_file, agent_config, field):
        path, _writes = machine_file
        mc.sync_machine_managed_config(WS, _authoritative(RAW), ["claude"])
        risky = {
            **RAW,
            "enabled_agents": [{"agent": "CODING_AGENT_CLAUDE_CODE", "config": agent_config}],
        }
        managed = _authoritative(risky)

        with pytest.raises(RuntimeError, match="may contain credentials") as exc:
            mc.sync_machine_managed_config(WS, managed, ["claude"])

        assert field in str(exc.value)
        payload = _published(path)
        # The earlier published copy is withdrawn rather than left authoritative.
        assert payload["outcome"] == "none"
        assert payload["config"] == {}
        assert "Authorization" not in path.read_text(encoding="utf-8")

    def test_ordinary_names_and_values_are_not_credentials(self):
        assert mc.machine_config_credential_fields(RAW) == []
        assert (
            mc.machine_config_credential_fields(
                {"auth": {"type": "oauth"}, "MAX_THINKING_TOKENS": "1024", "X-Team": "platform"}
            )
            == []
        )

    def test_a_failed_withdrawal_still_reports_the_refusal(self, machine_file, monkeypatch):
        path, _writes = machine_file
        mc.sync_machine_managed_config(WS, _authoritative(RAW), ["claude"])
        risky = {
            **RAW,
            "enabled_agents": [
                {
                    "agent": "CODING_AGENT_CLAUDE_CODE",
                    "config": {"http_headers": {"Authorization": "x"}},
                }
            ],
        }
        managed = _authoritative(risky)

        def fail(*_a, **_k):
            raise mf.ManagedFileWriteUnavailable("sudo was refused")

        monkeypatch.setattr(mc, "write_machine_file", fail)
        with pytest.raises(RuntimeError, match="may contain credentials") as exc:
            mc.sync_machine_managed_config(WS, managed, ["claude"])
        assert "could not be withdrawn" in str(exc.value)

    @pytest.mark.parametrize("skip", ["env", "windows"])
    def test_skipped_platforms_neither_warn_nor_write(self, machine_file, monkeypatch, skip):
        _path, writes = machine_file
        if skip == "env":
            monkeypatch.setenv(mf.SKIP_OS_SETTINGS_ENV, "1")
        else:
            monkeypatch.setattr(mc, "current_os", lambda: mf.OS.WINDOWS)
        risky = {
            **RAW,
            "enabled_agents": [
                {
                    "agent": "CODING_AGENT_CLAUDE_CODE",
                    "config": {"http_headers": {"Authorization": "x"}},
                }
            ],
        }

        status = mc.sync_machine_managed_config(WS, _authoritative(risky), ["claude"])

        assert status == ("skipped" if skip == "env" else "unsupported")
        assert writes == []

    def test_unreadable_existing_copy_is_invalidated(self, machine_file):
        path, _writes = machine_file
        path.write_text("{not json", encoding="utf-8")
        _authoritative(None, outcome="none")

        assert mc.sync_machine_managed_config(WS, None, []) == "invalidated"
        assert _published(path)["outcome"] == "none"

    def test_no_administrator_state_is_copied(self, machine_file):
        path, _writes = machine_file
        admin_state = {
            "workspace": WS,
            "profile": "admin-profile",
            "use_pat": True,
            "custom_oauth": {"client_id": "secret-client"},
            "relayed_proxy_port": 43210,
            "claude_models": {"opus": "system.ai.claude-opus-5"},
        }
        managed = _authoritative(RAW)

        cli_mod._sync_machine_managed_config(admin_state, managed, ["claude", "codex"])

        text = path.read_text(encoding="utf-8")
        for value in ("admin-profile", "secret-client", "43210", "use_pat", "claude_models"):
            assert value not in text
        assert set(_published(path)["ug_state"]) == {
            "version",
            "available_tools",
            "provider_services",
            "model_locations",
        }

    def test_dry_run_writes_nothing(self, monkeypatch, capsys):
        writes = MagicMock()
        monkeypatch.setattr(mf, "_sudo_replace", writes)
        monkeypatch.setattr(mf, "current_os", lambda: mf.OS.LINUX)
        config_io.set_dry_run(True)
        try:
            managed = _authoritative(RAW)
            assert mc.sync_machine_managed_config(WS, managed, ["claude"]) == "written"
        finally:
            config_io.set_dry_run(False)

        writes.assert_not_called()
        assert not mc.MACHINE_MANAGED_CONFIG_PATH.exists()
        assert "[dry run]" in capsys.readouterr().out


class TestConfigurePublishesMachineConfig:
    """`ug configure` calls the publisher with the agents that actually configured."""

    def test_managed_configure_publishes_only_configured_agents(self, monkeypatch):
        state = {"workspace": WS, "available_tools": []}
        managed = mc.normalize_managed_config(RAW)
        monkeypatch.setattr(cli_mod, "_configure_shared_workspace_states", lambda *a, **k: [state])
        monkeypatch.setattr(cli_mod, "save_state", lambda s: None)
        monkeypatch.setattr(cli_mod, "refresh_managed_config", lambda *a, **k: (managed, False))
        monkeypatch.setattr(cli_mod, "check_gateway_endpoint", lambda *a: True)
        monkeypatch.setattr(cli_mod, "install_tool_binary", lambda *a, **k: True)
        monkeypatch.setattr(cli_mod, "_configure_managed_mcp_servers", lambda *_a: [])
        monkeypatch.setattr(cli_mod, "_configure_managed_skills", lambda *_a: None)
        monkeypatch.setattr(cli_mod, "_summarize_managed_config", lambda *a, **k: None)
        # Codex fails to configure this run.
        monkeypatch.setattr(
            cli_mod,
            "configure_selected_tools",
            lambda resolved, tools, **_k: {
                **resolved,
                "last_configured_tools": [t for t in tools if t == "claude"],
            },
        )
        sync = MagicMock(return_value="created")
        monkeypatch.setattr(cli_mod, "sync_machine_managed_config", sync)

        assert cli_mod.configure_workspace_command(workspaces=[(WS, None)]) == 0

        sync.assert_called_once_with(WS, managed, ["claude"], keep_configured=False)

    def test_policy_whose_agents_all_fail_still_replaces_the_old_copy(self, monkeypatch):
        state = {"workspace": WS, "available_tools": []}
        managed = mc.normalize_managed_config(RAW)
        monkeypatch.setattr(cli_mod, "_configure_shared_workspace_states", lambda *a, **k: [state])
        monkeypatch.setattr(cli_mod, "save_state", lambda s: None)
        monkeypatch.setattr(cli_mod, "refresh_managed_config", lambda *a, **k: (managed, False))
        monkeypatch.setattr(cli_mod, "check_gateway_endpoint", lambda *a: True)
        monkeypatch.setattr(cli_mod, "install_tool_binary", lambda *a, **k: True)
        monkeypatch.setattr(
            cli_mod,
            "configure_selected_tools",
            lambda resolved, tools, **_k: {**resolved, "last_configured_tools": []},
        )
        sync = MagicMock(return_value="written")
        monkeypatch.setattr(cli_mod, "sync_machine_managed_config", sync)

        with pytest.raises(RuntimeError, match="None of the coding agents"):
            cli_mod._configure_workspace_command(workspaces=[(WS, None)])

        sync.assert_called_once_with(WS, managed, [], keep_configured=False)

    def test_unmanaged_configure_offers_invalidation(self, monkeypatch):
        state = {"workspace": WS, "available_tools": []}
        monkeypatch.setattr(cli_mod, "_configure_shared_workspace_states", lambda *a, **k: [state])
        monkeypatch.setattr(cli_mod, "save_state", lambda s: None)
        monkeypatch.setattr(cli_mod, "refresh_managed_config", lambda *a, **k: (None, False))
        monkeypatch.setattr(cli_mod, "check_gateway_endpoint", lambda *a: False)
        monkeypatch.setattr(cli_mod, "_print_discovery_diagnostics", lambda *_a: None)
        sync = MagicMock(return_value="invalidated")
        monkeypatch.setattr(cli_mod, "sync_machine_managed_config", sync)

        with pytest.raises(RuntimeError, match="No coding agents"):
            cli_mod._configure_workspace_command(selected_tools=["claude"], workspaces=[(WS, None)])

        sync.assert_called_once_with(WS, None, [], keep_configured=False)

    @pytest.mark.parametrize(("tool", "synced"), [("claude", True), ("gemini", False)])
    def test_single_agent_configure_keeps_earlier_agents(self, monkeypatch, tool, synced):
        state = {"workspace": WS}
        managed = mc.normalize_managed_config(RAW)
        monkeypatch.setattr(cli_mod, "_configure_shared_workspace_states", lambda *a, **k: [state])
        monkeypatch.setattr(cli_mod, "refresh_managed_config", lambda *a, **k: (managed, False))
        monkeypatch.setattr(cli_mod, "_reject_disabled_agent", lambda *a: None)
        monkeypatch.setattr(cli_mod, "configure_single_tool", lambda *a, **k: state)
        monkeypatch.setattr(cli_mod, "install_databricks_ai_tools_for_agents", lambda *a, **k: None)
        monkeypatch.setattr(cli_mod, "_record_unmanaged_configured_agents", lambda s, t: s)
        monkeypatch.setattr("ucode.desktop_setup.configure_claude_desktop", lambda *a, **k: None)
        sync = MagicMock(return_value="unchanged")
        monkeypatch.setattr(cli_mod, "sync_machine_managed_config", sync)

        cli_mod._setup_single_agent([(WS, None)], tool, apply_managed=True)

        if synced:
            sync.assert_called_once_with(WS, managed, [tool], keep_configured=True)
        else:
            # Only claude/codex read the managed config authoritatively here.
            sync.assert_not_called()

    def test_failed_privileged_write_is_reported_not_raised(self, monkeypatch, capsys):
        def fail(*_a, **_k):
            raise mf.ManagedFileWriteUnavailable("sudo was refused")

        monkeypatch.setattr(cli_mod, "sync_machine_managed_config", fail)

        cli_mod._sync_machine_managed_config({"workspace": WS}, {}, ["claude"])

        output = " ".join(capsys.readouterr().out.split())
        assert "Did not update the machine-wide config" in output
        assert "sudo was refused" in output
        assert "Saved the managed config machine-wide" not in output


class TestAuthoritativeRead:
    """Only a read that settled the question can publish or invalidate the machine copy."""

    def test_settled_reads_are_recorded(self, monkeypatch):
        monkeypatch.setattr(mc, "get_databricks_token", lambda *_a: "token")
        monkeypatch.setattr(
            mc, "get_managed_config", lambda *_a: mc.FetchedManagedConfig(RAW, None)
        )

        mc.refresh_managed_config({"workspace": WS}, force_refresh=True)

        read = mc.authoritative_managed_read(WS)
        assert read is not None
        assert read["outcome"] == "published"
        assert read["config"] == RAW

    def test_a_failed_fetch_that_falls_back_records_nothing(self, monkeypatch):
        monkeypatch.setattr(mc, "get_databricks_token", lambda *_a: "token")
        monkeypatch.setattr(
            mc, "get_managed_config", lambda *_a: mc.FetchedManagedConfig(RAW, None)
        )
        mc.refresh_managed_config({"workspace": WS}, force_refresh=True)
        monkeypatch.setattr(
            mc, "get_managed_config", lambda *_a: mc.FetchedManagedConfig(None, "503 unavailable")
        )

        result = mc.refresh_managed_config({"workspace": WS}, force_refresh=True)

        # The cached policy still applies to this user, but it is not a fresh answer.
        assert result.manifest is not None
        assert mc.authoritative_managed_read(WS) is None
