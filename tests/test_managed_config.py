"""Tests for managed_config.py — fetch/normalize/persist of the admin-authored managed config."""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest

import ucode.config_io as config_io_mod
import ucode.databricks as db_mod
import ucode.managed_config as mc_mod
from ucode.managed_config import (
    get_managed_config,
    load_managed_configuration,
    load_managed_state,
    managed_config_is_newer,
    managed_state_workspace,
    managed_update_time,
    normalize_managed_config,
    refresh_managed_config,
    save_managed_state,
)
from ucode.managed_setup import serialize_managed_config
from ucode.telemetry import client_platform

# A CodingAgentConfig in the current agent-config wire shape as emitted by ai-gateway-api.
# The wire format uses: default_models map (not separate default_model + default_alias_models),
# model field names (model_services, unity_catalog_location), tier field names
# (recommended_agent, recommended_model), and mcp_servers/skills as a names-or-location selector.
RAW_MANIFEST = {
    "spec_version": 1,
    "retrieved_time": "2026-09-09T22:00:00Z",
    "default_agent": "CODING_AGENT_CLAUDE_CODE",
    "enabled_agents": [
        {
            "agent": "CODING_AGENT_CLAUDE_CODE",
            "config": {
                "models": {
                    "model_services": [
                        "system.ai.claude-opus-4-8",
                        "system.ai.claude-sonnet-4-6",
                        "system.ai.claude-haiku-4-5",
                    ],
                },
                "default_models": {
                    "default_model": "system.ai.claude-opus-4-8",
                    "default_opus_model": "system.ai.claude-opus-4-8",
                    "default_sonnet_model": "system.ai.claude-sonnet-4-6",
                    "default_haiku_model": "system.ai.claude-haiku-4-5",
                },
                "smart_routing": {"enabled": True},
                "tracing": {"enabled": True},
                "http_headers": {"x-databricks-workspace": "eng-ml-inference"},
                "agent_native_settings": {"allowManagedMcpServersOnly": True, "env": {"FOO": "1"}},
            },
        },
        {
            "agent": "CODING_AGENT_CODEX",
            "config": {
                "models": {"model_provider_service": "main.default.openai-mps"},
                "default_models": {"default_model": "gpt-5.4"},
            },
        },
    ],
    "mcp_servers": {"names": ["system.ai.github", "main.default.jira"]},
    "skills": {"names": ["system.ai.pdf-extraction"]},
    "smart_defaults": {
        "budget_id": "c6563b45-df9a-4b19-afb2-d42dc2b52576",
        "tiers": [
            {
                "spending_percentage": 0.9,
                "recommended_agent": "CODING_AGENT_CODEX",
                "recommended_model": "gpt-5.4",
            },
        ],
    },
}

# A legacy cache/API response from before the server renamed ``spend_tiers`` to
# ``smart_defaults``. It is accepted only when the current key is absent.
LEGACY_RAW_MANIFEST = {
    **{key: value for key, value in RAW_MANIFEST.items() if key != "smart_defaults"},
    "spend_tiers": RAW_MANIFEST["smart_defaults"],
}


class TestNormalize:
    def test_maps_agents_to_tool_names(self):
        cfg = normalize_managed_config(RAW_MANIFEST)
        assert cfg["default_agent"] == "claude"
        assert set(cfg["enabled_agents"]) == {"claude", "codex"}

    def test_claude_alias_models_map_to_family_slots(self):
        claude = normalize_managed_config(RAW_MANIFEST)["enabled_agents"]["claude"]
        assert claude["model_config"]["default_models_by_model_family"] == {
            "default_opus_model": "system.ai.claude-opus-4-8",
            "default_sonnet_model": "system.ai.claude-sonnet-4-6",
            "default_haiku_model": "system.ai.claude-haiku-4-5",
        }
        assert claude["model_config"]["default_model"] == "system.ai.claude-opus-4-8"

    def test_http_headers_map_to_http_headers(self):
        claude = normalize_managed_config(RAW_MANIFEST)["enabled_agents"]["claude"]
        assert claude["http_headers"] == {"x-databricks-workspace": "eng-ml-inference"}

    def test_settings_are_carried_verbatim(self):
        agents = normalize_managed_config(RAW_MANIFEST)["enabled_agents"]
        assert agents["claude"]["agent_native_settings"] == {
            "allowManagedMcpServersOnly": True,
            "env": {"FOO": "1"},
        }
        assert "agent_native_settings" not in agents["codex"]

    def test_non_object_settings_are_dropped(self):
        raw = {
            "enabled_agents": [
                {"agent": "CODING_AGENT_CODEX", "config": {"agent_native_settings": "x"}}
            ]
        }
        assert (
            "agent_native_settings" not in normalize_managed_config(raw)["enabled_agents"]["codex"]
        )

    def test_smart_routing_maps_to_agent_switch(self):
        claude = normalize_managed_config(RAW_MANIFEST)["enabled_agents"]["claude"]
        assert claude["smart_routing_enabled"] is True

    def test_per_agent_tracing_enabled_is_carried(self):
        claude = normalize_managed_config(RAW_MANIFEST)["enabled_agents"]["claude"]
        assert claude["otel_tracing_enabled"] is True

    def test_per_agent_tracing_disabled_is_carried(self):
        raw = {
            "enabled_agents": [
                {
                    "agent": "CODING_AGENT_CODEX",
                    "config": {"tracing": {"enabled": False}},
                }
            ]
        }
        codex = normalize_managed_config(raw)["enabled_agents"]["codex"]
        assert codex["otel_tracing_enabled"] is False

    def test_workspace_level_tracing_is_ignored(self):
        assert "otel_tracing_enabled" not in normalize_managed_config(
            {"tracing": {"enabled": True}}
        )

    def test_static_model_services_are_carried(self):
        claude = normalize_managed_config(RAW_MANIFEST)["enabled_agents"]["claude"]
        assert claude["model_config"]["model_services"] == [
            "system.ai.claude-opus-4-8",
            "system.ai.claude-sonnet-4-6",
            "system.ai.claude-haiku-4-5",
        ]

    def test_unity_catalog_location_is_carried(self):
        raw = {
            "spec_version": 1,
            "enabled_agents": [
                {
                    "agent": "CODING_AGENT_CLAUDE_CODE",
                    "config": {"models": {"unity_catalog_location": "main.agents"}},
                }
            ],
        }
        claude = normalize_managed_config(raw)["enabled_agents"]["claude"]
        assert claude["model_config"]["unity_catalog_location"] == "main.agents"

    def test_mcp_servers_and_skills_carry_names(self):
        cfg = normalize_managed_config(RAW_MANIFEST)
        assert cfg["mcp_servers"] == {"names": ["system.ai.github", "main.default.jira"]}
        assert cfg["skills"] == {"names": ["system.ai.pdf-extraction"]}

    def test_mcp_servers_and_skills_carry_location(self):
        raw = {
            "mcp_servers": {"unity_catalog_location": "main.mcp"},
            "skills": {"unity_catalog_location": "main.skills"},
        }
        cfg = normalize_managed_config(raw)
        assert cfg["mcp_servers"] == {"unity_catalog_location": "main.mcp"}
        assert cfg["skills"] == {"unity_catalog_location": "main.skills"}

    def test_codex_provider_service(self):
        codex = normalize_managed_config(RAW_MANIFEST)["enabled_agents"]["codex"]
        assert codex["model_config"]["model_provider_service"] == "main.default.openai-mps"
        assert codex["model_config"]["default_model"] == "gpt-5.4"

    def test_smart_defaults_carries_budget_id_and_tiers(self):
        cfg = normalize_managed_config(RAW_MANIFEST)
        assert cfg["smart_defaults"]["budget_id"] == "c6563b45-df9a-4b19-afb2-d42dc2b52576"
        assert cfg["smart_defaults"]["tiers"][0]["recommended_agent"] == "codex"

    def test_legacy_spend_tiers_fall_back_to_smart_defaults(self):
        cfg = normalize_managed_config(LEGACY_RAW_MANIFEST)
        assert cfg["smart_defaults"] == normalize_managed_config(RAW_MANIFEST)["smart_defaults"]
        assert "spend_tiers" not in cfg

    def test_smart_defaults_take_precedence_over_legacy_spend_tiers(self):
        raw = {
            **LEGACY_RAW_MANIFEST,
            "smart_defaults": {"budget_id": "new-budget"},
        }
        assert normalize_managed_config(raw)["smart_defaults"] == {"budget_id": "new-budget"}

    @pytest.mark.parametrize("smart_defaults", [None, {}, {"tiers": []}])
    def test_explicit_empty_smart_defaults_do_not_resurrect_legacy(self, smart_defaults):
        raw = {**LEGACY_RAW_MANIFEST, "smart_defaults": smart_defaults}
        assert "smart_defaults" not in normalize_managed_config(raw)

    def test_spec_version_not_carried_into_internal_manifest(self):
        # Kept out so the serialize/normalize round trip (which never sees spec_version) is unaffected.
        assert "spec_version" not in normalize_managed_config(RAW_MANIFEST)


class TestGetManagedConfig:
    def test_returns_the_first_config_raw(self, monkeypatch):
        # get_managed_config returns the config verbatim now; normalization happens on read.
        monkeypatch.setattr(
            mc_mod,
            "fetch_managed_coding_agent_configs",
            lambda ws, tok: ([RAW_MANIFEST], None),
        )
        cfg, reason = get_managed_config("https://ws", "tok")
        assert reason is None
        assert cfg == RAW_MANIFEST

    def test_no_config_is_not_an_error(self, monkeypatch):
        monkeypatch.setattr(
            mc_mod, "fetch_managed_coding_agent_configs", lambda ws, tok: ([], None)
        )
        cfg, reason = get_managed_config("https://ws", "tok")
        assert cfg is None
        assert reason is None

    def test_fetch_failure_surfaces_reason(self, monkeypatch):
        monkeypatch.setattr(
            mc_mod,
            "fetch_managed_coding_agent_configs",
            lambda ws, tok: ([], "HTTP 500 Server Error"),
        )
        cfg, reason = get_managed_config("https://ws", "tok")
        assert cfg is None
        assert reason == "HTTP 500 Server Error"

    @pytest.mark.parametrize(
        "not_found_reason",
        [
            "HTTP 404 Not Found",
            'HTTP 404 Not Found: {"error_code":"NOT_FOUND","message":"..."}',
            'HTTP 400 Bad Request: {"error_code":"NOT_FOUND"}',
        ],
    )
    def test_not_found_is_treated_as_no_config(self, monkeypatch, not_found_reason):
        # A NOT_FOUND from the read means the admin hasn't defined a config — the normal
        # no-config case, not an error, so it collapses to (None, None).
        monkeypatch.setattr(
            mc_mod,
            "fetch_managed_coding_agent_configs",
            lambda ws, tok: ([], not_found_reason),
        )
        cfg, reason = get_managed_config("https://ws", "tok")
        assert cfg is None
        assert reason is None

    def test_feature_disabled_is_not_swallowed_as_not_found(self, monkeypatch):
        reason = (
            'HTTP 404 Not Found: {"error_code":"FEATURE_DISABLED",'
            '"message":"Coding agent config APIs are not enabled for this workspace."}'
        )
        monkeypatch.setattr(
            mc_mod, "fetch_managed_coding_agent_configs", lambda ws, tok: ([], reason)
        )
        cfg, reason_out = get_managed_config("https://ws", "tok")
        assert cfg is None
        assert reason_out == reason

    def test_returns_raw_wire_shape(self, monkeypatch):
        # The raw wire config is returned unchanged (enabled_agents stays a list of {agent, config});
        # normalize_managed_config's translation is covered by its own tests.
        monkeypatch.setattr(
            mc_mod, "fetch_managed_coding_agent_configs", lambda ws, tok: ([RAW_MANIFEST], None)
        )
        cfg, reason = get_managed_config("https://ws", "tok")
        assert reason is None
        assert cfg == RAW_MANIFEST

    def test_spec_version_newer_than_supported_is_refused(self, monkeypatch):
        raw = {**RAW_MANIFEST, "spec_version": mc_mod.MAX_SPEC_VERSION + 1}
        monkeypatch.setattr(
            mc_mod, "fetch_managed_coding_agent_configs", lambda ws, tok: ([raw], None)
        )
        cfg, reason = get_managed_config("https://ws", "tok")
        # Refused (reason set) rather than misread, so the launch path keeps the last-known-good
        # cache instead of applying a config it can't parse.
        assert cfg is None
        assert reason is not None and "spec_version" in reason

    @pytest.mark.parametrize("bad_spec", ["2", 2.0, True])
    def test_malformed_spec_version_is_refused(self, monkeypatch, bad_spec):
        raw = {**RAW_MANIFEST, "spec_version": bad_spec}
        monkeypatch.setattr(
            mc_mod, "fetch_managed_coding_agent_configs", lambda ws, tok: ([raw], None)
        )
        cfg, reason = get_managed_config("https://ws", "tok")
        assert cfg is None
        assert reason is not None and "spec_version" in reason


class TestManagedConfigStub:
    def test_stub_short_circuits_the_http_read(self, tmp_path, monkeypatch):
        stub = tmp_path / "managed.json"
        stub.write_text(json.dumps(RAW_MANIFEST), encoding="utf-8")
        monkeypatch.setenv("UCODE_MANAGED_CONFIG_STUB", str(stub))

        def _fail(ws, tok):
            raise AssertionError("stub set: the HTTP read must not run")

        monkeypatch.setattr(mc_mod, "fetch_managed_coding_agent_configs", _fail)
        cfg, reason = get_managed_config("https://ws", "tok")
        assert reason is None
        assert cfg == RAW_MANIFEST

    def test_stub_applies_the_spec_version_gate(self, tmp_path, monkeypatch):
        stub = tmp_path / "managed.json"
        stub.write_text(
            json.dumps({**RAW_MANIFEST, "spec_version": mc_mod.MAX_SPEC_VERSION + 1}),
            encoding="utf-8",
        )
        monkeypatch.setenv("UCODE_MANAGED_CONFIG_STUB", str(stub))
        cfg, reason = get_managed_config("https://ws", "tok")
        assert cfg is None
        assert reason is not None and "spec_version" in reason

    def test_unreadable_stub_falls_through_to_the_http_read(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UCODE_MANAGED_CONFIG_STUB", str(tmp_path / "missing.json"))
        monkeypatch.setattr(
            mc_mod, "fetch_managed_coding_agent_configs", lambda ws, tok: ([RAW_MANIFEST], None)
        )
        cfg, reason = get_managed_config("https://ws", "tok")
        assert reason is None
        assert cfg == RAW_MANIFEST

    def test_null_stub_reproduces_a_workspace_with_no_managed_config(self, tmp_path, monkeypatch):
        stub = tmp_path / "managed.json"
        stub.write_text("null", encoding="utf-8")
        monkeypatch.setenv("UCODE_MANAGED_CONFIG_STUB", str(stub))

        def _fail(ws, tok):
            raise AssertionError("stub set: the HTTP read must not run")

        monkeypatch.setattr(mc_mod, "fetch_managed_coding_agent_configs", _fail)
        cfg, reason = get_managed_config("https://ws", "tok")
        assert cfg is None
        assert reason is None


class TestUnsupportedSpecFallback:
    def test_unsupported_spec_warns_on_cold_launch(self, monkeypatch):
        # No cache + a too-new spec_version is proof a policy exists, so surface it rather than
        # silently treating the workspace as having no managed config.
        warnings: list = []
        monkeypatch.setattr(mc_mod, "print_warning", lambda msg: warnings.append(msg))
        monkeypatch.setattr(mc_mod, "load_managed_state", lambda ws: None)
        reason = "This workspace's managed config needs a newer Unity Gateway (spec_version 2)."
        assert mc_mod._persisted_fallback("https://ws", reason) is None
        assert warnings and "spec_version" in warnings[0]

    def test_transient_failure_stays_silent_on_cold_launch(self, monkeypatch):
        warnings: list = []
        monkeypatch.setattr(mc_mod, "print_warning", lambda msg: warnings.append(msg))
        monkeypatch.setattr(mc_mod, "load_managed_state", lambda ws: None)
        assert mc_mod._persisted_fallback("https://ws", "HTTP 503 Service Unavailable") is None
        assert warnings == []


class TestPersistence:
    @pytest.fixture(autouse=True)
    def _managed_path(self, tmp_path, monkeypatch):
        path = tmp_path / ".ucode" / "managed-config.json"
        monkeypatch.setattr(mc_mod, "MANAGED_CONFIG_PATH", path)
        return path

    def test_save_stores_raw_and_load_normalizes(self, _managed_path):
        # The file holds the raw config verbatim; load_managed_state normalizes on read.
        save_managed_state("https://ws.example.com", RAW_MANIFEST)
        assert load_managed_configuration("https://ws.example.com") == RAW_MANIFEST
        assert load_managed_state("https://ws.example.com") == normalize_managed_config(
            RAW_MANIFEST
        )

    @pytest.mark.skipif(sys.platform == "win32", reason="Windows has no POSIX file modes")
    def test_saved_file_is_0644(self, _managed_path):
        save_managed_state("https://ws.example.com", {"default_agent": "claude"})
        mode = stat.S_IMODE(os.stat(_managed_path).st_mode)
        assert mode == 0o644

    def test_load_ignores_other_workspace(self, _managed_path):
        save_managed_state("https://ws-a.example.com", {"default_agent": "claude"})
        assert load_managed_state("https://ws-b.example.com") is None

    def test_load_missing_returns_none(self, _managed_path):
        assert load_managed_state("https://ws.example.com") is None

    def test_load_none_workspace_returns_none(self, _managed_path):
        assert load_managed_state(None) is None

    def test_empty_config_removes_a_matching_policy(self, _managed_path):
        save_managed_state("https://ws.example.com", {"default_agent": "claude"})
        save_managed_state("https://ws.example.com", {})
        assert load_managed_state("https://ws.example.com") is None
        assert not _managed_path.exists()

    def test_workspace_is_stored_alongside_the_config(self, _managed_path):
        # `ucode setup --show` reads this when local state carries no workspace yet, so the authored
        # file can still be found and attributed on disk.
        save_managed_state("https://ws.example.com", {"default_agent": "claude"})
        assert managed_state_workspace() == "https://ws.example.com"

    def test_workspace_is_none_when_absent(self, _managed_path):
        assert managed_state_workspace() is None

    def test_policy_contains_only_workspace_and_raw_config(self, _managed_path):
        save_managed_state("https://ws.example.com", RAW_MANIFEST)
        stored = json.loads(_managed_path.read_text(encoding="utf-8"))
        assert stored == {"workspace": "https://ws.example.com", "config": RAW_MANIFEST}

    def test_dry_run_writes_nothing(self, _managed_path, monkeypatch):
        # Under --dry-run the config writers print instead of touching disk, so a launch that
        # dry-runs an admin's authored draft never overwrites it.
        monkeypatch.setattr(config_io_mod, "is_dry_run", lambda: True)
        save_managed_state("https://ws.example.com", {"default_agent": "claude"})
        assert not _managed_path.exists()

    def test_corrupt_file_reads_as_absent(self, _managed_path):
        # A truncated/hand-mangled file must not crash a launch: it reads as "no config" so the
        # launch falls through rather than raising on JSON it can't parse.
        _managed_path.parent.mkdir(parents=True, exist_ok=True)
        _managed_path.write_text("{not json", encoding="utf-8")
        assert load_managed_state("https://ws.example.com") is None
        assert managed_state_workspace() is None

    def test_loaded_config_serializes_to_a_json_encodable_payload(self, _managed_path):
        # `ucode publish` POSTs the serialized config, so a manifest that survives a disk round-trip
        # must still serialize to something json.dumps accepts with no custom encoder.
        cfg = normalize_managed_config(RAW_MANIFEST)
        save_managed_state("https://ws.example.com", cfg)
        loaded = load_managed_state("https://ws.example.com")
        assert loaded is not None
        assert json.loads(json.dumps(serialize_managed_config(loaded)))


class TestFetchClient:
    """fetch_managed_coding_agent_configs lives in databricks.py; test its response parsing."""

    def test_extracts_configs_list(self, monkeypatch):
        payload = {"coding_agent_configs": [RAW_MANIFEST]}
        monkeypatch.setattr(
            db_mod,
            "_http_get_json",
            lambda url, token, timeout=10, headers=None: (payload, None),
        )
        configs, reason = db_mod.fetch_managed_coding_agent_configs("https://ws", "tok")
        assert reason is None
        assert len(configs) == 1
        assert configs[0]["default_agent"] == "CODING_AGENT_CLAUDE_CODE"

    def test_empty_list_when_no_configs(self, monkeypatch):
        monkeypatch.setattr(
            db_mod,
            "_http_get_json",
            lambda url, token, timeout=10, headers=None: ({}, None),
        )
        configs, reason = db_mod.fetch_managed_coding_agent_configs("https://ws", "tok")
        assert configs == []
        assert reason is None

    def test_http_failure_surfaces_reason(self, monkeypatch):
        monkeypatch.setattr(
            db_mod,
            "_http_get_json",
            lambda url, token, timeout=10, headers=None: (None, "HTTP 403 Forbidden"),
        )
        configs, reason = db_mod.fetch_managed_coding_agent_configs("https://ws", "tok")
        assert configs == []
        assert reason == "HTTP 403 Forbidden"

    @pytest.mark.parametrize(
        ("claude_ua", "codex_ua", "expected"),
        [
            (None, None, "ucode/9.9.9"),
            ("ucode/1.0 claude/2.1.289", None, "ucode/9.9.9 claude/2.1.289"),
            (
                "ucode/1.0 claude/2.1.289",
                "ucode/1.0 codex/0.154.0",
                "ucode/9.9.9 claude/2.1.289 codex/0.154.0",
            ),
            ("admin-agent/1.0", "ucode/1.0 claude/2.1.289", "ucode/9.9.9"),
        ],
    )
    def test_sends_ucode_user_agent_header(self, monkeypatch, claude_ua, codex_ua, expected):
        from ucode.agents import claude, codex

        monkeypatch.setattr(db_mod, "ug_version", lambda: "9.9.9")
        monkeypatch.setattr(claude, "managed_user_agent", lambda: claude_ua)
        monkeypatch.setattr(codex, "managed_user_agent", lambda: codex_ua)
        captured = {}

        class _FakeResponse:
            headers: dict[str, str] = {}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b'{"coding_agent_configs": []}'

        def fake_urlopen(request, timeout=None):
            captured["request"] = request
            return _FakeResponse()

        monkeypatch.setattr(db_mod.urllib_request, "urlopen", fake_urlopen)
        db_mod.fetch_managed_coding_agent_configs("https://ws", "tok")
        assert captured["request"].get_header("User-agent") == f"{expected} {client_platform()}"


WORKSPACE = "https://ws.example.com"

# A normalized managed config, as `normalize_managed_config` produces it.
MANAGED = {
    "default_agent": "claude",
    "enabled_agents": {
        "claude": {
            "model_config": {
                "default_model": "system.ai.claude-opus-5",
                "default_models_by_model_family": {"default_opus_model": "system.ai.claude-opus-5"},
            }
        }
    },
}


def _state(**overrides) -> dict:
    state = {"workspace": WORKSPACE, "managed_configs": {"claude": {"keys": []}}}
    state.update(overrides)
    return state


class TestRefreshManagedConfig:
    """The per-launch re-read, so an admin's edits land without re-running `ucode configure`."""

    @pytest.fixture(autouse=True)
    def _stub_token(self, monkeypatch):
        monkeypatch.setattr(mc_mod, "get_databricks_token", lambda ws, profile: "tok")

    def test_persists_raw_and_returns_the_normalized_manifest(self, monkeypatch):
        saved: list[tuple] = []
        monkeypatch.setattr(mc_mod, "get_managed_config", lambda ws, tok: (RAW_MANIFEST, None))
        monkeypatch.setattr(
            mc_mod, "save_managed_state", lambda ws, cfg, **kwargs: saved.append((ws, cfg))
        )
        # The raw config is persisted verbatim; the caller gets the normalized manifest.
        assert refresh_managed_config(_state(), force_refresh=True) == (
            normalize_managed_config(RAW_MANIFEST),
            False,
        )
        assert saved == [(WORKSPACE, RAW_MANIFEST)]

    def test_shared_host_binds_the_policy_to_the_workspace(self, monkeypatch):
        """On a host serving several workspaces the policy is stored for one of them."""
        fetched: list[str] = []
        saved: list[tuple] = []
        monkeypatch.setattr(
            mc_mod,
            "get_managed_config",
            lambda ws, tok: fetched.append(ws) or (RAW_MANIFEST, None),
        )
        monkeypatch.setattr(
            mc_mod, "save_managed_state", lambda ws, cfg, **kwargs: saved.append((ws, cfg))
        )
        shared = "https://acme.cloud.databricks.com"
        refresh_managed_config(_state(workspace=shared, workspace_id="111"), force_refresh=True)
        assert fetched == [shared]
        assert saved == [(f"{shared}?w=111", RAW_MANIFEST)]

    def test_policy_saved_for_a_pasted_url_matches_its_saved_workspace(self, monkeypatch):
        from ucode.managed_config import managed_launch_workspace

        pasted = "https://adb-1.azuredatabricks.net/?o=1"
        mc_mod.MANAGED_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        mc_mod.MANAGED_CONFIG_PATH.write_text(
            json.dumps({"workspace": pasted, "config": {}}), encoding="utf-8"
        )
        assert managed_launch_workspace(pasted) == pasted

    def test_no_managed_config_returns_none(self, monkeypatch):
        monkeypatch.setattr(mc_mod, "get_managed_config", lambda ws, tok: (None, None))
        monkeypatch.setattr(mc_mod, "save_managed_state", lambda ws, cfg, **kwargs: None)
        result, _ = refresh_managed_config(_state())
        assert result is None

    def test_read_failure_falls_back_to_the_persisted_config(self, monkeypatch):
        # The admin's last known policy beats no policy, so a failed fetch reuses what we saved.
        warnings: list[str] = []
        monkeypatch.setattr(mc_mod, "get_managed_config", lambda ws, tok: (None, "HTTP 500"))
        monkeypatch.setattr(mc_mod, "load_managed_state", lambda ws: MANAGED)
        monkeypatch.setattr(mc_mod, "print_warning", lambda msg: warnings.append(msg))
        assert refresh_managed_config(_state()) == (MANAGED, False)
        assert "HTTP 500" in warnings[0]
        assert "last one saved" in warnings[0]

    def test_read_failure_without_persisted_config_is_silent(self, monkeypatch):
        # Nothing persisted means no managed config is in play, so an expired session shouldn't
        # produce a warning about a feature this developer doesn't use.
        monkeypatch.setattr(mc_mod, "get_managed_config", lambda ws, tok: (None, "HTTP 500"))
        monkeypatch.setattr(mc_mod, "load_managed_state", lambda ws: None)
        monkeypatch.setattr(
            mc_mod, "print_warning", lambda msg: pytest.fail(f"should not warn: {msg}")
        )
        result, _ = refresh_managed_config(_state())
        assert result is None

    def test_auth_failure_falls_back_to_the_persisted_config(self, monkeypatch):
        warnings: list[str] = []

        def boom(ws, profile):
            raise RuntimeError("no token")

        monkeypatch.setattr(mc_mod, "get_databricks_token", boom)
        monkeypatch.setattr(mc_mod, "load_managed_state", lambda ws: MANAGED)
        monkeypatch.setattr(mc_mod, "print_warning", lambda msg: warnings.append(msg))
        assert refresh_managed_config(_state()) == (MANAGED, False)
        assert "no token" in warnings[0]

    def test_auth_failure_without_persisted_config_is_silent(self, monkeypatch):
        def boom(ws, profile):
            raise RuntimeError("no token")

        monkeypatch.setattr(mc_mod, "get_databricks_token", boom)
        monkeypatch.setattr(mc_mod, "load_managed_state", lambda ws: None)
        monkeypatch.setattr(
            mc_mod, "print_warning", lambda msg: pytest.fail(f"should not warn: {msg}")
        )
        result, _ = refresh_managed_config(_state())
        assert result is None

    def test_permission_denied_without_cache_is_silent(self, monkeypatch):
        # A refusal is no evidence a config exists, so with nothing cached there is no managed
        # config in play and warning would be a false positive.
        denied = 'HTTP 403 Forbidden: {"error_code":"PERMISSION_DENIED"}'
        monkeypatch.setattr(mc_mod, "get_managed_config", lambda ws, tok: (None, denied))
        monkeypatch.setattr(mc_mod, "load_managed_state", lambda ws: None)
        monkeypatch.setattr(
            mc_mod, "print_warning", lambda msg: pytest.fail(f"should not warn: {msg}")
        )
        result, _ = refresh_managed_config(_state())
        assert result is None

    def test_permission_denied_warns_and_keeps_the_cached_config(self, monkeypatch):
        # A refused read is worth surfacing: an admin may have published a config that isn't
        # reaching this developer. It says nothing about whether one exists, so the cache stands.
        warnings: list[str] = []
        denied = 'HTTP 403 Forbidden: {"error_code":"PERMISSION_DENIED"}'
        monkeypatch.setattr(mc_mod, "get_managed_config", lambda ws, tok: (None, denied))
        monkeypatch.setattr(mc_mod, "load_managed_state", lambda ws: MANAGED)
        monkeypatch.setattr(mc_mod, "print_warning", lambda msg: warnings.append(msg))
        monkeypatch.setattr(
            mc_mod,
            "save_managed_state",
            lambda ws, cfg, **kwargs: pytest.fail("must not clear the cache"),
        )
        assert refresh_managed_config(_state()) == (MANAGED, False)
        assert "not readable by you" in warnings[0]

    def test_no_config_on_the_server_does_not_use_a_stale_persisted_file(self, monkeypatch):
        # A successful read saying "no config" means the admin removed it — that's authoritative,
        # so a previously persisted file must not resurrect the old policy.
        monkeypatch.setattr(mc_mod, "get_managed_config", lambda ws, tok: (None, None))
        monkeypatch.setattr(mc_mod, "save_managed_state", lambda ws, cfg, **kwargs: None)
        monkeypatch.setattr(
            mc_mod, "load_managed_state", lambda ws: pytest.fail("must not fall back")
        )
        result, _ = refresh_managed_config(_state())
        assert result is None

    def test_no_config_on_the_server_clears_the_persisted_one(self, monkeypatch):
        # Without this, removing the config server-side would leave the old one on disk and the next
        # failed read would put a dead policy back into force.
        saved: list[tuple] = []
        monkeypatch.setattr(mc_mod, "get_managed_config", lambda ws, tok: (None, None))
        monkeypatch.setattr(
            mc_mod, "save_managed_state", lambda ws, cfg, **kwargs: saved.append((ws, cfg))
        )
        monkeypatch.setattr(mc_mod, "load_managed_state", lambda ws: None)
        result, _ = refresh_managed_config(_state(), force_refresh=True)
        assert result is None
        assert saved == [(WORKSPACE, {})]

    def test_empty_persisted_config_is_not_treated_as_a_fallback(self, monkeypatch):
        # The empty marker means "no admin policy", so a later failed read falls through to the
        # developer's own settings rather than reporting a managed config.
        monkeypatch.setattr(mc_mod, "get_managed_config", lambda ws, tok: (None, "HTTP 500"))
        monkeypatch.setattr(mc_mod, "load_managed_state", lambda ws: {})
        monkeypatch.setattr(
            mc_mod, "print_warning", lambda msg: pytest.fail(f"should not warn: {msg}")
        )
        result, _ = refresh_managed_config(_state())
        assert result is None

    def test_no_workspace_is_a_noop(self, monkeypatch):
        monkeypatch.setattr(
            mc_mod, "get_managed_config", lambda ws, tok: pytest.fail("should not fetch")
        )
        result, _ = refresh_managed_config({})
        assert result is None

    def test_feature_disabled_sets_flag_when_there_is_no_fallback(self, monkeypatch):
        # The workspace hasn't enabled coding-agent-configs server-side, so `ucode setup` can't
        # publish anything yet. The flag lets callers suppress the setup recommendation.
        reason = 'HTTP 400 Bad Request: {"error_code":"FEATURE_DISABLED"}'
        monkeypatch.setattr(mc_mod, "get_managed_config", lambda ws, tok: (None, reason))
        monkeypatch.setattr(mc_mod, "load_managed_state", lambda ws: None)
        monkeypatch.setattr(mc_mod, "print_warning", lambda msg: None)
        state = _state()
        result, flag = refresh_managed_config(state)
        assert result is None
        assert flag is True

    def test_feature_disabled_ignores_a_cached_config_and_sets_the_flag(self, monkeypatch):
        # FEATURE_DISABLED is authoritative, so a config cached while the feature was enabled no
        # longer applies: report "no config, feature off" and clear the persisted copy so a later
        # transient failure can't resurrect the disabled policy via the fallback.
        saved: list[tuple] = []
        reason = 'HTTP 400 Bad Request: {"error_code":"FEATURE_DISABLED"}'
        monkeypatch.setattr(mc_mod, "get_managed_config", lambda ws, tok: (None, reason))
        monkeypatch.setattr(mc_mod, "load_managed_state", lambda ws: MANAGED)
        monkeypatch.setattr(
            mc_mod, "save_managed_state", lambda ws, cfg, **kwargs: saved.append((ws, cfg))
        )
        monkeypatch.setattr(
            mc_mod,
            "print_warning",
            lambda msg: pytest.fail("feature-disabled must not warn about falling back to a cache"),
        )
        state = _state()
        result, flag = refresh_managed_config(state, force_refresh=True)
        assert result is None
        assert flag is True
        assert saved == [(WORKSPACE, {})]

    def test_transient_failure_does_not_set_the_flag(self, monkeypatch):
        monkeypatch.setattr(mc_mod, "get_managed_config", lambda ws, tok: (None, "HTTP 500"))
        monkeypatch.setattr(mc_mod, "load_managed_state", lambda ws: None)
        monkeypatch.setattr(mc_mod, "print_warning", lambda msg: None)
        state = _state()
        result, flag = refresh_managed_config(state)
        assert result is None
        assert flag is False

    def test_successful_no_config_clears_the_flag(self, monkeypatch):
        monkeypatch.setattr(mc_mod, "get_managed_config", lambda ws, tok: (None, None))
        monkeypatch.setattr(mc_mod, "save_managed_state", lambda ws, cfg, **kwargs: None)
        state = _state()
        result, flag = refresh_managed_config(state)
        assert result is None
        assert flag is False


class TestLaunchReplaysSavedConfig:
    """A launch replays the config `ug configure` saved however old it is and never refetches;
    `ug configure` (force_refresh) and a launch with nothing saved for the workspace read fresh."""

    @pytest.fixture(autouse=True)
    def _stub_token(self, monkeypatch):
        monkeypatch.setattr(mc_mod, "get_databricks_token", lambda ws, profile: "tok")

    @staticmethod
    def _counting_fetch(monkeypatch, result=(RAW_MANIFEST, None)):
        calls = {"n": 0}

        def fetch(ws, tok):
            calls["n"] += 1
            return result

        monkeypatch.setattr(mc_mod, "get_managed_config", fetch)
        return calls

    @staticmethod
    def _write_saved(*, config, outcome, workspace=WORKSPACE, extra=None):
        save_managed_state(workspace, config)
        entry = {
            "outcome": outcome,
            "policy_fingerprint": mc_mod.managed_file_fingerprint(mc_mod.MANAGED_CONFIG_PATH),
            **(extra or {}),
        }
        mc_mod.REFRESH_TS_PATH.write_text(json.dumps({workspace: entry}), encoding="utf-8")

    @staticmethod
    def _no_fetch(monkeypatch):
        monkeypatch.setattr(
            mc_mod,
            "get_managed_config",
            lambda ws, tok: pytest.fail("a launch must not refetch a saved managed config"),
        )
        monkeypatch.setattr(
            mc_mod,
            "get_databricks_token",
            lambda ws, profile: pytest.fail("a launch with a saved config needs no token"),
        )

    def test_saved_published_config_is_replayed(self, monkeypatch):
        self._write_saved(config=RAW_MANIFEST, outcome="published")
        self._no_fetch(monkeypatch)
        assert refresh_managed_config(_state()) == (normalize_managed_config(RAW_MANIFEST), False)

    def test_old_saved_config_is_not_refetched(self, monkeypatch):
        # The former expiry stamp, however old, no longer triggers a refetch.
        self._write_saved(
            config=RAW_MANIFEST,
            outcome="published",
            extra={"last_checked_at": "2000-01-01T00:00:00+00:00"},
        )
        self._no_fetch(monkeypatch)
        assert refresh_managed_config(_state()) == (normalize_managed_config(RAW_MANIFEST), False)

    def test_saved_legacy_config_normalizes_to_smart_defaults(self, monkeypatch):
        self._write_saved(config=LEGACY_RAW_MANIFEST, outcome="published")
        self._no_fetch(monkeypatch)
        result, feature_disabled = refresh_managed_config(_state())
        assert result == normalize_managed_config(LEGACY_RAW_MANIFEST)
        assert result is not None
        assert "smart_defaults" in result
        assert "spend_tiers" not in result
        assert feature_disabled is False

    def test_saved_no_config_is_replayed(self, monkeypatch):
        self._write_saved(config={}, outcome="none")
        self._no_fetch(monkeypatch)
        assert refresh_managed_config(_state()) == (None, False)

    def test_saved_feature_disabled_is_replayed(self, monkeypatch):
        self._write_saved(config={}, outcome="feature_disabled")
        self._no_fetch(monkeypatch)
        assert refresh_managed_config(_state()) == (None, True)

    def test_other_workspace_save_is_fetched(self, monkeypatch):
        self._write_saved(config={}, outcome="none", workspace="https://other.example.com")
        calls = self._counting_fetch(monkeypatch)
        refresh_managed_config(_state())
        assert calls["n"] == 1

    def test_force_refresh_bypasses_the_saved_config(self, monkeypatch):
        # `ug configure` passes force_refresh=True so it never applies a since-changed config.
        self._write_saved(config=RAW_MANIFEST, outcome="published")
        calls = self._counting_fetch(monkeypatch)
        refresh_managed_config(_state(), force_refresh=True)
        assert calls["n"] == 1

    def test_configure_fetches_fresh_and_saves_for_the_next_launch(self, monkeypatch):
        calls = self._counting_fetch(monkeypatch)
        refresh_managed_config(_state(), force_refresh=True)
        assert calls["n"] == 1
        assert load_managed_configuration(WORKSPACE) == RAW_MANIFEST
        self._no_fetch(monkeypatch)
        assert refresh_managed_config(_state()) == (normalize_managed_config(RAW_MANIFEST), False)

    def test_nothing_saved_fetches_until_configure_installs_it(self, monkeypatch):
        """A launch never installs, so with nothing installed each launch reads the policy for that
        run only; once `ug configure` installs it, launches replay it."""
        calls = self._counting_fetch(monkeypatch)
        refresh_managed_config(_state())
        refresh_managed_config(_state())
        assert calls["n"] == 2
        assert not mc_mod.MANAGED_CONFIG_PATH.exists()

        refresh_managed_config(_state(), force_refresh=True)
        refresh_managed_config(_state())
        assert calls["n"] == 3

    def test_non_utf8_saved_file_falls_through_to_a_fetch(self, monkeypatch):
        # A corrupted (non-UTF-8) file must not crash: it reads as absent.
        mc_mod.MANAGED_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        mc_mod.MANAGED_CONFIG_PATH.write_bytes(b"\xff\xfe not utf-8")
        calls = self._counting_fetch(monkeypatch)
        refresh_managed_config(_state())
        assert calls["n"] == 1

    def test_successful_read_records_outcome_without_a_time_stamp(self, monkeypatch):
        self._counting_fetch(monkeypatch)
        refresh_managed_config(_state(), force_refresh=True)
        stored = json.loads(mc_mod.MANAGED_CONFIG_PATH.read_text(encoding="utf-8"))
        assert stored["config"] == RAW_MANIFEST
        assert set(stored) == {"workspace", "config"}
        entry = json.loads(mc_mod.REFRESH_TS_PATH.read_text())[WORKSPACE]
        assert entry["outcome"] == "published"
        assert set(entry) == {"outcome", "policy_fingerprint"}
        if sys.platform != "win32":  # Windows has no POSIX file modes
            assert stat.S_IMODE(mc_mod.REFRESH_TS_PATH.stat().st_mode) == 0o600

    def test_no_config_read_records_none_outcome(self, monkeypatch):
        self._counting_fetch(monkeypatch, result=(None, None))
        refresh_managed_config(_state(), force_refresh=True)
        assert not mc_mod.MANAGED_CONFIG_PATH.exists()
        stored = json.loads(mc_mod.REFRESH_TS_PATH.read_text())[WORKSPACE]
        assert stored["outcome"] == "none"

    def test_feature_disabled_read_records_feature_disabled_outcome(self, monkeypatch):
        reason = 'HTTP 400 Bad Request: {"error_code":"FEATURE_DISABLED"}'
        self._counting_fetch(monkeypatch, result=(None, reason))
        monkeypatch.setattr(mc_mod, "print_warning", lambda msg: None)
        refresh_managed_config(_state(), force_refresh=True)
        assert not mc_mod.MANAGED_CONFIG_PATH.exists()
        stored = json.loads(mc_mod.REFRESH_TS_PATH.read_text())[WORKSPACE]
        assert stored["outcome"] == "feature_disabled"

    def test_failed_forced_read_keeps_the_saved_record(self, monkeypatch):
        # A transient failure falls back to the last good config and leaves the record untouched.
        self._write_saved(config=RAW_MANIFEST, outcome="published")
        before = mc_mod.REFRESH_TS_PATH.read_text()
        self._counting_fetch(monkeypatch, result=(None, "HTTP 500"))
        monkeypatch.setattr(mc_mod, "print_warning", lambda msg: None)
        result, flag = refresh_managed_config(_state(), force_refresh=True)
        assert result == normalize_managed_config(RAW_MANIFEST)
        assert flag is False
        assert mc_mod.REFRESH_TS_PATH.read_text() == before


class TestMachinePolicyRefresh:
    @pytest.fixture(autouse=True)
    def _stub_fetch(self, monkeypatch):
        monkeypatch.setattr(mc_mod, "get_databricks_token", lambda ws, profile: "tok")
        monkeypatch.setattr(mc_mod, "get_managed_config", lambda ws, token: (RAW_MANIFEST, None))

    def test_unchanged_policy_never_invokes_privileged_writer(self, monkeypatch):
        save_managed_state(WORKSPACE, RAW_MANIFEST)
        before = mc_mod.MANAGED_CONFIG_PATH.stat()
        original = mc_mod.MANAGED_CONFIG_PATH.read_bytes()
        monkeypatch.setattr(
            mc_mod,
            "update_workspace_policy",
            lambda *args, **kwargs: pytest.fail("unchanged policy must not invoke sudo"),
        )
        assert refresh_managed_config(
            _state(), force_refresh=True
        ).manifest == normalize_managed_config(RAW_MANIFEST)
        after = mc_mod.MANAGED_CONFIG_PATH.stat()
        assert mc_mod.MANAGED_CONFIG_PATH.read_bytes() == original
        assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
        entry = json.loads(mc_mod.REFRESH_TS_PATH.read_text())[WORKSPACE]
        assert set(entry) == {"outcome", "policy_fingerprint"}

    def test_configure_with_unchanged_policy_rewrites_only_the_user_record(self, monkeypatch):
        refresh_managed_config(_state(), force_refresh=True)
        original = mc_mod.managed_file_fingerprint(mc_mod.MANAGED_CONFIG_PATH)
        monkeypatch.setattr(
            mc_mod,
            "update_workspace_policy",
            lambda *args, **kwargs: pytest.fail("refresh must not rewrite unchanged policy"),
        )
        refresh_managed_config(_state(), force_refresh=True)
        assert mc_mod.managed_file_fingerprint(mc_mod.MANAGED_CONFIG_PATH) == original
        assert json.loads(mc_mod.REFRESH_TS_PATH.read_text())[WORKSPACE]["outcome"] == "published"

    def test_server_retrieval_time_change_does_not_trigger_sudo(self, monkeypatch):
        save_managed_state(WORKSPACE, RAW_MANIFEST)
        checked = {**RAW_MANIFEST, "retrieved_time": "2026-09-16T12:00:00+00:00"}
        monkeypatch.setattr(mc_mod, "get_managed_config", lambda ws, token: (checked, None))
        monkeypatch.setattr(
            mc_mod,
            "update_workspace_policy",
            lambda *args, **kwargs: pytest.fail("server retrieval metadata is not policy drift"),
        )
        assert refresh_managed_config(_state()).manifest == normalize_managed_config(checked)
        assert load_managed_configuration(WORKSPACE) == RAW_MANIFEST

    @pytest.mark.parametrize("force_refresh", [False, True])
    def test_only_explicit_configure_allows_policy_elevation(self, monkeypatch, force_refresh):
        writes = []
        installer = mc_mod.update_workspace_policy

        def install(path, desired_text, *, interactive=False, allow_sudo=True):
            writes.append((interactive, allow_sudo))
            installer(path, desired_text, interactive=interactive, allow_sudo=allow_sudo)

        monkeypatch.setattr(mc_mod, "update_workspace_policy", install)
        refresh_managed_config(_state(), force_refresh=force_refresh)
        assert writes == ([(True, True)] if force_refresh else [])

    def test_denied_changed_policy_warns_and_reuses_installed_policy(self, monkeypatch):
        save_managed_state(WORKSPACE, RAW_MANIFEST)
        changed = {**RAW_MANIFEST, "default_agent": "CODING_AGENT_CODEX"}
        monkeypatch.setattr(mc_mod, "get_managed_config", lambda ws, token: (changed, None))
        warnings = []

        def denied(*args, **kwargs):
            raise RuntimeError("sudo permission denied")

        monkeypatch.setattr(mc_mod, "update_workspace_policy", denied)
        monkeypatch.setattr(mc_mod, "print_warning", warnings.append)
        result = refresh_managed_config(_state())
        assert result == (normalize_managed_config(RAW_MANIFEST), False)
        assert load_managed_configuration(WORKSPACE) == RAW_MANIFEST
        assert "Ask your IT team or an administrator" in warnings[0]
        assert "Continuing with the installed policy" in warnings[0]
        assert "run `ug configure`" not in warnings[0]
        monkeypatch.setattr(
            mc_mod,
            "get_managed_config",
            lambda *args: pytest.fail("a checked-but-uninstalled change must still be throttled"),
        )
        assert refresh_managed_config(_state()) == result

    @pytest.mark.parametrize("is_root", [False, True])
    def test_launch_applies_the_fetched_policy_without_installing(self, monkeypatch, is_root):
        """A first launch before any configure is managed and installs nothing, even as root."""
        monkeypatch.setattr(mc_mod.system_cross_os, "is_admin", lambda: is_root)
        warnings = []
        monkeypatch.setattr(mc_mod, "print_warning", warnings.append)
        monkeypatch.setattr(
            mc_mod,
            "update_workspace_policy",
            lambda *a, **k: pytest.fail("a launch must not install the machine policy"),
        )

        result = refresh_managed_config(_state())

        assert result.manifest == normalize_managed_config(RAW_MANIFEST)
        assert not mc_mod.MANAGED_CONFIG_PATH.exists()
        assert warnings == []

    def test_configure_installs_the_policy(self):
        refresh_managed_config(_state(), force_refresh=True)

        assert load_managed_configuration(WORKSPACE) == RAW_MANIFEST

    def test_configure_reports_failed_install_and_preserves_policy(self, monkeypatch):
        save_managed_state(WORKSPACE, RAW_MANIFEST)
        changed = {**RAW_MANIFEST, "default_agent": "CODING_AGENT_CODEX"}
        monkeypatch.setattr(mc_mod, "get_managed_config", lambda ws, token: (changed, None))

        def denied(*args, **kwargs):
            raise RuntimeError("sudo permission denied")

        monkeypatch.setattr(mc_mod, "update_workspace_policy", denied)
        with pytest.raises(RuntimeError, match="run `ug configure`"):
            refresh_managed_config(_state(), force_refresh=True)
        assert load_managed_configuration(WORKSPACE) == RAW_MANIFEST
        assert not mc_mod.REFRESH_TS_PATH.exists()

    def test_no_remote_policy_never_creates_machine_file(self, monkeypatch):
        monkeypatch.setattr(mc_mod, "get_managed_config", lambda ws, token: (None, None))
        monkeypatch.setattr(
            mc_mod,
            "update_workspace_policy",
            lambda *args, **kwargs: pytest.fail("no policy must not invoke sudo"),
        )
        assert refresh_managed_config(_state()) == (None, False)
        assert not mc_mod.MANAGED_CONFIG_PATH.parent.exists()
        assert json.loads(mc_mod.REFRESH_TS_PATH.read_text())[WORKSPACE]["outcome"] == "none"

    def test_removed_policy_is_deleted_instead_of_replaced_with_empty_file(self, monkeypatch):
        save_managed_state(WORKSPACE, RAW_MANIFEST)
        monkeypatch.setattr(mc_mod, "get_managed_config", lambda ws, token: (None, None))
        assert refresh_managed_config(_state(), force_refresh=True) == (None, False)
        assert not mc_mod.MANAGED_CONFIG_PATH.exists()

    @pytest.mark.parametrize("is_root", [False, True])
    def test_launch_never_removes_the_installed_policy(self, monkeypatch, is_root):
        save_managed_state(WORKSPACE, RAW_MANIFEST)
        monkeypatch.setattr(mc_mod, "get_managed_config", lambda ws, token: (None, None))
        monkeypatch.setattr(mc_mod.system_cross_os, "is_admin", lambda: is_root)
        monkeypatch.setattr(
            mc_mod,
            "update_workspace_policy",
            lambda *a, **k: pytest.fail("a launch must not remove the machine policy"),
        )
        warnings = []
        monkeypatch.setattr(mc_mod, "print_warning", warnings.append)
        result = refresh_managed_config(_state())
        assert result.manifest == normalize_managed_config(RAW_MANIFEST)
        assert mc_mod.MANAGED_CONFIG_PATH.exists()
        if is_root:
            assert "Run `ug configure`" in warnings[0]
            assert "IT team" not in warnings[0]
        else:
            assert "Ask your IT team or an administrator" in warnings[0]

    @pytest.mark.parametrize("reason", [None, "FEATURE_DISABLED"])
    def test_failed_policy_removal_warns_and_preserves_installed_policy(self, monkeypatch, reason):
        save_managed_state(WORKSPACE, RAW_MANIFEST)
        monkeypatch.setattr(mc_mod, "get_managed_config", lambda ws, token: (None, reason))
        warnings = []

        def denied(*args, **kwargs):
            raise RuntimeError("sudo permission denied")

        monkeypatch.setattr(mc_mod, "update_workspace_policy", denied)
        monkeypatch.setattr(mc_mod, "print_warning", warnings.append)
        # A disabled feature stays reported, so callers still skip the setup recommendation.
        feature_disabled = reason is not None and "FEATURE_DISABLED" in reason
        assert refresh_managed_config(_state()) == (
            normalize_managed_config(RAW_MANIFEST),
            feature_disabled,
        )
        assert load_managed_configuration(WORKSPACE) == RAW_MANIFEST
        assert warnings

    def test_cached_feature_disabled_read_keeps_its_flag_when_removal_failed(self, monkeypatch):
        save_managed_state(WORKSPACE, RAW_MANIFEST)
        monkeypatch.setattr(
            mc_mod, "get_managed_config", lambda ws, token: (None, "FEATURE_DISABLED")
        )

        def denied(*args, **kwargs):
            raise RuntimeError("sudo permission denied")

        monkeypatch.setattr(mc_mod, "update_workspace_policy", denied)
        monkeypatch.setattr(mc_mod, "print_warning", lambda *_a: None)
        first = refresh_managed_config(_state())
        monkeypatch.setattr(
            mc_mod, "get_managed_config", lambda *_a: pytest.fail("a fresh stamp must be reused")
        )

        assert (
            refresh_managed_config(_state())
            == first
            == (
                normalize_managed_config(RAW_MANIFEST),
                True,
            )
        )

    def test_configure_for_a_workspace_without_policy_removes_another_workspaces(self, monkeypatch):
        """`ug configure` decides the machine's workspace, even when it publishes no policy."""
        save_managed_state("https://other.example.com", RAW_MANIFEST)
        monkeypatch.setattr(mc_mod, "get_managed_config", lambda ws, token: (None, None))

        assert refresh_managed_config(_state(), force_refresh=True) == (None, False)

        assert not mc_mod.MANAGED_CONFIG_PATH.exists()
        assert managed_state_workspace() is None

    def test_launch_never_removes_another_workspaces_policy(self):
        """Only `ug configure` changes the machine's workspace; a launch is refused instead."""
        save_managed_state("https://other.example.com", RAW_MANIFEST)

        with pytest.raises(RuntimeError, match="Launch refused"):
            refresh_managed_config(_state())

        assert load_managed_configuration("https://other.example.com") == RAW_MANIFEST

    def test_launch_for_another_workspace_without_policy_keeps_it(self):
        """Only an explicit configure may remove another workspace's policy."""
        save_managed_state("https://other.example.com", RAW_MANIFEST)

        save_managed_state(WORKSPACE, {}, allow_sudo=False)

        assert load_managed_configuration("https://other.example.com") == RAW_MANIFEST

    def test_timestamps_are_keyed_by_workspace_without_policy_copies(self, monkeypatch):
        monkeypatch.setattr(mc_mod, "get_managed_config", lambda ws, token: (None, None))
        refresh_managed_config(_state())
        refresh_managed_config({"workspace": "https://other.example.com"})
        entries = json.loads(mc_mod.REFRESH_TS_PATH.read_text())
        assert set(entries) == {WORKSPACE, "https://other.example.com"}
        assert all("config" not in entry for entry in entries.values())

    def test_workspace_urls_with_trailing_slash_share_policy_and_timestamp(self, monkeypatch):
        refresh_managed_config(_state(), force_refresh=True)
        monkeypatch.setattr(
            mc_mod,
            "get_managed_config",
            lambda *args: pytest.fail("canonical workspace should reuse the check"),
        )
        assert refresh_managed_config({"workspace": WORKSPACE + "/"}) == (
            normalize_managed_config(RAW_MANIFEST),
            False,
        )

    def test_external_policy_change_invalidates_recent_stamp(self, monkeypatch):
        refresh_managed_config(_state())
        changed = {**RAW_MANIFEST, "default_agent": "CODING_AGENT_CODEX"}
        save_managed_state(WORKSPACE, changed)
        fetches = []

        def fetch(*args):
            fetches.append(args)
            return changed, None

        monkeypatch.setattr(mc_mod, "get_managed_config", fetch)
        assert refresh_managed_config(_state()).manifest == normalize_managed_config(changed)
        assert len(fetches) == 1

    def test_legacy_home_cache_is_not_a_policy_fallback(self, monkeypatch):
        legacy = config_io_mod.APP_DIR / "managed-config.json"
        legacy.write_text(json.dumps({"workspace": WORKSPACE, "config": RAW_MANIFEST}))
        monkeypatch.setattr(mc_mod, "get_managed_config", lambda ws, token: (None, "HTTP 503"))
        assert refresh_managed_config(_state()) == (None, False)
        assert not mc_mod.MANAGED_CONFIG_PATH.exists()

    def test_launch_dry_run_does_not_fetch_or_stamp(self, monkeypatch):
        save_managed_state(WORKSPACE, RAW_MANIFEST)
        monkeypatch.setattr(config_io_mod, "is_dry_run", lambda: True)
        monkeypatch.setattr(
            mc_mod, "get_managed_config", lambda *args: pytest.fail("dry run must be offline")
        )
        assert refresh_managed_config(_state()) == (normalize_managed_config(RAW_MANIFEST), False)
        assert not mc_mod.REFRESH_TS_PATH.exists()

    @staticmethod
    def _invalidate_saved_read():
        """Make the next launch look like nothing is saved, as when the installed file changed."""
        stamps = json.loads(mc_mod.REFRESH_TS_PATH.read_text())
        stamps[WORKSPACE]["policy_fingerprint"] = None
        mc_mod.REFRESH_TS_PATH.write_text(json.dumps(stamps))

    @staticmethod
    def _deny_policy_updates(monkeypatch):
        warnings = []

        def denied(*args, **kwargs):
            raise RuntimeError("administrator update required")

        monkeypatch.setattr(mc_mod, "update_workspace_policy", denied)
        monkeypatch.setattr(mc_mod, "print_warning", warnings.append)
        return warnings

    def test_same_pending_change_warns_once_across_rechecks(self, monkeypatch):
        save_managed_state(WORKSPACE, RAW_MANIFEST)
        original = mc_mod.managed_file_fingerprint(mc_mod.MANAGED_CONFIG_PATH)
        changed = {**RAW_MANIFEST, "default_agent": "CODING_AGENT_CODEX"}
        fetches = []

        def fetch(workspace, token):
            fetches.append(workspace)
            return changed, None

        monkeypatch.setattr(mc_mod, "get_managed_config", fetch)
        warnings = self._deny_policy_updates(monkeypatch)
        for rechecks in range(4):
            if rechecks:
                self._invalidate_saved_read()
            assert refresh_managed_config(_state()) == (
                normalize_managed_config(RAW_MANIFEST),
                False,
            )
            entry = json.loads(mc_mod.REFRESH_TS_PATH.read_text())[WORKSPACE]
            assert len(entry["pending_config_hash"]) == 64
            assert "config" not in entry
        assert fetches == [WORKSPACE] * 4
        assert len(warnings) == 1
        assert mc_mod.managed_file_fingerprint(mc_mod.MANAGED_CONFIG_PATH) == original
        if sys.platform != "win32":  # Windows has no POSIX file modes
            assert stat.S_IMODE(mc_mod.REFRESH_TS_PATH.stat().st_mode) == 0o600

    def test_distinct_pending_change_warns_again(self, monkeypatch):
        save_managed_state(WORKSPACE, RAW_MANIFEST)
        warnings = self._deny_policy_updates(monkeypatch)
        hashes = []
        for index, default_agent in enumerate(("CODING_AGENT_CODEX", "CODING_AGENT_GEMINI")):
            changed = {**RAW_MANIFEST, "default_agent": default_agent}
            monkeypatch.setattr(
                mc_mod, "get_managed_config", lambda *args, changed=changed: (changed, None)
            )
            if index:
                self._invalidate_saved_read()
            refresh_managed_config(_state())
            hashes.append(
                json.loads(mc_mod.REFRESH_TS_PATH.read_text())[WORKSPACE]["pending_config_hash"]
            )
        assert len(warnings) == 2
        assert hashes[0] != hashes[1]

    def test_pending_hash_ignores_retrieval_time_and_object_key_order(self, monkeypatch):
        save_managed_state(WORKSPACE, RAW_MANIFEST)
        changed = {**RAW_MANIFEST, "default_agent": "CODING_AGENT_CODEX"}
        warnings = self._deny_policy_updates(monkeypatch)
        monkeypatch.setattr(mc_mod, "get_managed_config", lambda *args: (changed, None))
        refresh_managed_config(_state())
        first_hash = json.loads(mc_mod.REFRESH_TS_PATH.read_text())[WORKSPACE][
            "pending_config_hash"
        ]
        checked = dict(
            reversed(list({**changed, "retrieved_time": "2026-09-16T12:00:00+00:00"}.items()))
        )
        self._invalidate_saved_read()
        monkeypatch.setattr(mc_mod, "get_managed_config", lambda *args: (checked, None))
        refresh_managed_config(_state())
        assert len(warnings) == 1
        assert (
            json.loads(mc_mod.REFRESH_TS_PATH.read_text())[WORKSPACE]["pending_config_hash"]
            == first_hash
        )

    @pytest.mark.parametrize("reason", [None, "FEATURE_DISABLED"])
    def test_pending_removal_warns_once_across_rechecks(self, monkeypatch, reason):
        save_managed_state(WORKSPACE, RAW_MANIFEST)
        warnings = self._deny_policy_updates(monkeypatch)
        monkeypatch.setattr(mc_mod, "get_managed_config", lambda *args: (None, reason))
        for rechecks in range(3):
            if rechecks:
                self._invalidate_saved_read()
            assert refresh_managed_config(_state()).manifest == normalize_managed_config(
                RAW_MANIFEST
            )
        assert len(warnings) == 1
        assert "pending_config_hash" in json.loads(mc_mod.REFRESH_TS_PATH.read_text())[WORKSPACE]

    @pytest.mark.parametrize("reconciled_by", ["remote", "mdm", "configure"])
    def test_reconciliation_clears_pending_hash(self, monkeypatch, reconciled_by):
        save_managed_state(WORKSPACE, RAW_MANIFEST)
        installer = mc_mod.update_workspace_policy
        changed = {**RAW_MANIFEST, "default_agent": "CODING_AGENT_CODEX"}
        monkeypatch.setattr(mc_mod, "get_managed_config", lambda *args: (changed, None))
        warnings = self._deny_policy_updates(monkeypatch)
        refresh_managed_config(_state())
        assert "pending_config_hash" in json.loads(mc_mod.REFRESH_TS_PATH.read_text())[WORKSPACE]
        self._invalidate_saved_read()
        if reconciled_by == "remote":
            monkeypatch.setattr(mc_mod, "get_managed_config", lambda *args: (RAW_MANIFEST, None))
        elif reconciled_by == "mdm":
            installer(
                mc_mod.MANAGED_CONFIG_PATH,
                json.dumps({"workspace": WORKSPACE, "config": changed}),
            )
        else:
            monkeypatch.setattr(mc_mod, "update_workspace_policy", installer)
        refresh_managed_config(_state(), force_refresh=reconciled_by == "configure")
        assert len(warnings) == 1
        assert (
            "pending_config_hash" not in json.loads(mc_mod.REFRESH_TS_PATH.read_text())[WORKSPACE]
        )

    def test_reintroduced_change_warns_after_remote_reverts(self, monkeypatch):
        save_managed_state(WORKSPACE, RAW_MANIFEST)
        changed = {**RAW_MANIFEST, "default_agent": "CODING_AGENT_CODEX"}
        warnings = self._deny_policy_updates(monkeypatch)
        for index, remote in enumerate((changed, RAW_MANIFEST, changed)):
            if index:
                self._invalidate_saved_read()
            monkeypatch.setattr(
                mc_mod, "get_managed_config", lambda *args, remote=remote: (remote, None)
            )
            refresh_managed_config(_state())
        assert len(warnings) == 2

    def test_fetch_failure_preserves_pending_hash_and_record(self, monkeypatch):
        save_managed_state(WORKSPACE, RAW_MANIFEST)
        changed = {**RAW_MANIFEST, "default_agent": "CODING_AGENT_CODEX"}
        monkeypatch.setattr(mc_mod, "get_managed_config", lambda *args: (changed, None))
        self._deny_policy_updates(monkeypatch)
        refresh_managed_config(_state())
        self._invalidate_saved_read()
        original = mc_mod.REFRESH_TS_PATH.read_bytes()
        monkeypatch.setattr(mc_mod, "get_managed_config", lambda *args: (None, "HTTP 503"))
        refresh_managed_config(_state())
        assert mc_mod.REFRESH_TS_PATH.read_bytes() == original


class TestPolicyWithoutInstallation:
    """Windows and UG_SKIP_OS_SETTINGS can't hold an installed policy."""

    @pytest.fixture(autouse=True)
    def _stub_fetch(self, monkeypatch):
        monkeypatch.setattr(mc_mod, "get_databricks_token", lambda ws, profile: "tok")
        monkeypatch.setattr(mc_mod, "get_managed_config", lambda ws, token: (RAW_MANIFEST, None))

    @pytest.fixture(params=["skip_os_settings", "windows"])
    def not_installable(self, request, monkeypatch):
        if request.param == "skip_os_settings":
            monkeypatch.setenv("UG_SKIP_OS_SETTINGS", "1")
        else:
            monkeypatch.setattr(mc_mod, "current_os", lambda: mc_mod.OS.WINDOWS)
        monkeypatch.setattr(
            mc_mod,
            "update_workspace_policy",
            lambda *a, **k: pytest.fail("nothing may be installed where policy can't be"),
        )

    @pytest.mark.parametrize("force_refresh", [False, True])
    def test_fetched_policy_applies_without_installing(self, not_installable, force_refresh):
        result = refresh_managed_config(_state(), force_refresh=force_refresh)

        assert result.manifest == normalize_managed_config(RAW_MANIFEST)
        assert not mc_mod.MANAGED_CONFIG_PATH.exists()

    def test_launch_replays_the_per_user_cache(self, not_installable, monkeypatch):
        """A saved read is replayed on launch, however old, instead of fetching again."""
        refresh_managed_config(_state(), force_refresh=True)
        monkeypatch.setattr(
            mc_mod, "get_managed_config", lambda *_a: pytest.fail("a saved read must be replayed")
        )
        monkeypatch.setattr(
            mc_mod, "get_databricks_token", lambda *_a: pytest.fail("a replay needs no token")
        )

        result = refresh_managed_config(_state())

        assert result.manifest == normalize_managed_config(RAW_MANIFEST)
        assert mc_mod.USER_MANAGED_CONFIG_PATH.exists()
        assert not mc_mod.MANAGED_CONFIG_PATH.exists()

    def test_per_user_cache_records_outcome_without_a_time_stamp(self, not_installable):
        refresh_managed_config(_state(), force_refresh=True)

        stored = json.loads(mc_mod.USER_MANAGED_CONFIG_PATH.read_text(encoding="utf-8"))
        assert stored == {"workspace": WORKSPACE, "config": RAW_MANIFEST, "outcome": "published"}

    def test_per_user_cache_written_by_an_older_ug_is_replayed(self, not_installable, monkeypatch):
        mc_mod.USER_MANAGED_CONFIG_PATH.write_text(
            json.dumps(
                {
                    "workspace": WORKSPACE,
                    "config": RAW_MANIFEST,
                    "outcome": "published",
                    "retrieved_at": "2000-01-01T00:00:00+00:00",
                }
            )
        )
        monkeypatch.setattr(
            mc_mod, "get_managed_config", lambda *_a: pytest.fail("a saved read must be replayed")
        )

        assert refresh_managed_config(_state()) == (normalize_managed_config(RAW_MANIFEST), False)

    @pytest.mark.parametrize(
        ("saved", "expected"),
        [
            ({"outcome": "none", "config": {}}, (None, False)),
            ({"outcome": "feature_disabled", "config": {}}, (None, True)),
        ],
    )
    def test_per_user_cache_replays_empty_outcomes(
        self, not_installable, monkeypatch, saved, expected
    ):
        mc_mod.USER_MANAGED_CONFIG_PATH.write_text(json.dumps({"workspace": WORKSPACE, **saved}))
        monkeypatch.setattr(
            mc_mod, "get_managed_config", lambda *_a: pytest.fail("a saved read must be replayed")
        )

        assert refresh_managed_config(_state()) == expected

    def test_per_user_cache_without_an_outcome_is_read_once(self, not_installable, monkeypatch):
        """An older empty file could be "no config" or a disabled feature; stamp it before trusting."""
        mc_mod.USER_MANAGED_CONFIG_PATH.write_text(
            json.dumps({"workspace": WORKSPACE, "config": {}})
        )
        fetched: list[str] = []
        monkeypatch.setattr(
            mc_mod,
            "get_managed_config",
            lambda ws, _tok: fetched.append(ws) or (None, "FEATURE_DISABLED"),
        )

        assert refresh_managed_config(_state()) == (None, True)
        assert fetched == [WORKSPACE]
        monkeypatch.setattr(
            mc_mod, "get_managed_config", lambda *_a: pytest.fail("the stamped read must replay")
        )
        assert refresh_managed_config(_state()) == (None, True)

    def test_failed_fetch_falls_back_to_the_per_user_cache(self, not_installable, monkeypatch):
        """Unchanged from main: an outage keeps the last saved policy in force."""
        refresh_managed_config(_state())
        monkeypatch.setattr(mc_mod, "get_managed_config", lambda *_a: (None, "503: unavailable"))

        result = refresh_managed_config(_state(), force_refresh=True)

        assert result.manifest == normalize_managed_config(RAW_MANIFEST)

    def test_removed_policy_replaces_the_per_user_cache(self, not_installable, monkeypatch):
        refresh_managed_config(_state())
        monkeypatch.setattr(mc_mod, "get_managed_config", lambda *_a: (None, None))

        assert refresh_managed_config(_state(), force_refresh=True) == (None, False)
        assert load_managed_configuration(WORKSPACE) == {}

    @pytest.mark.skipif(sys.platform == "win32", reason="Windows has no POSIX file modes")
    def test_per_user_cache_is_private(self, not_installable):
        refresh_managed_config(_state())

        assert stat.S_IMODE(mc_mod.USER_MANAGED_CONFIG_PATH.stat().st_mode) == 0o600

    def test_windows_ignores_a_planted_posix_policy_file(self, monkeypatch):
        """On Windows the POSIX path sits under a drive root a standard user may create."""
        save_managed_state("https://planted.example.com", {"default_agent": "CODING_AGENT_CODEX"})
        monkeypatch.setattr(mc_mod, "current_os", lambda: mc_mod.OS.WINDOWS)

        result = refresh_managed_config(_state())

        assert result.manifest == normalize_managed_config(RAW_MANIFEST)
        assert mc_mod.managed_state_workspace() is None
        assert load_managed_configuration("https://planted.example.com") is None

    def test_export_finds_the_per_user_cache_without_a_saved_workspace(self, not_installable):
        refresh_managed_config(_state())

        assert mc_mod.managed_state_workspace() is None
        assert mc_mod.managed_state_workspace(include_user_cache=True) == WORKSPACE

    def test_installed_policy_still_wins_with_skip(self, monkeypatch, capsys):
        save_managed_state(WORKSPACE, RAW_MANIFEST)
        monkeypatch.setenv("UG_SKIP_OS_SETTINGS", "1")
        changed = {**RAW_MANIFEST, "default_agent": "CODING_AGENT_CODEX"}
        monkeypatch.setattr(mc_mod, "get_managed_config", lambda ws, token: (changed, None))

        def unavailable(*_a, **_k):
            raise RuntimeError(
                "Machine-wide workspace policy is supported only on Linux and macOS."
            )

        monkeypatch.setattr(mc_mod, "update_workspace_policy", unavailable)

        result = refresh_managed_config(_state())

        assert result.manifest == normalize_managed_config(RAW_MANIFEST)
        assert load_managed_configuration(WORKSPACE) == RAW_MANIFEST
        output = " ".join(capsys.readouterr().out.split())
        assert "UG_SKIP_OS_SETTINGS is set" in output
        assert "Continuing with the installed policy" in output

    def test_configure_with_skip_degrades_like_a_launch(self, monkeypatch, capsys):
        """The installed policy can't be updated with the opt-out, so configure warns, not fails."""
        save_managed_state(WORKSPACE, RAW_MANIFEST)
        monkeypatch.setenv("UG_SKIP_OS_SETTINGS", "1")
        changed = {**RAW_MANIFEST, "default_agent": "CODING_AGENT_CODEX"}
        monkeypatch.setattr(mc_mod, "get_managed_config", lambda ws, token: (changed, None))

        def unavailable(*_a, **_k):
            raise RuntimeError(
                "Machine-wide workspace policy is supported only on Linux and macOS."
            )

        monkeypatch.setattr(mc_mod, "update_workspace_policy", unavailable)

        result = refresh_managed_config(_state(), force_refresh=True)

        assert result.manifest == normalize_managed_config(RAW_MANIFEST)
        assert "UG_SKIP_OS_SETTINGS is set" in " ".join(capsys.readouterr().out.split())


class TestInstalledWorkspaceLaunch:
    @pytest.mark.parametrize("dry_run", [False, True])
    def test_mismatched_launch_is_refused_before_cache_auth_fetch_or_write(
        self, monkeypatch, dry_run
    ):
        save_managed_state("https://installed.example.com", RAW_MANIFEST)
        original = mc_mod.MANAGED_CONFIG_PATH.read_bytes()
        monkeypatch.setattr(config_io_mod, "is_dry_run", lambda: dry_run)
        for helper in (
            "_saved_result",
            "_saved_user_result",
            "get_databricks_token",
            "get_managed_config",
            "save_managed_state",
        ):
            monkeypatch.setattr(
                mc_mod, helper, lambda *args, **kwargs: pytest.fail("mismatch must fail first")
            )
        with pytest.raises(RuntimeError, match="Launch refused") as error:
            refresh_managed_config(_state())
        assert "https://installed.example.com" in str(error.value)
        assert WORKSPACE in str(error.value)
        assert "IT team or an administrator" in str(error.value)
        assert mc_mod.MANAGED_CONFIG_PATH.read_bytes() == original
        assert not mc_mod.REFRESH_TS_PATH.exists()

    @pytest.mark.parametrize(
        "selected", [None, WORKSPACE, WORKSPACE + "/", WORKSPACE.removeprefix("https://")]
    )
    def test_installed_workspace_is_the_refresh_source(self, monkeypatch, selected):
        save_managed_state(WORKSPACE, RAW_MANIFEST)
        calls = []

        def token(workspace, profile):
            assert workspace == WORKSPACE
            assert profile == "developer"
            return "tok"

        def fetch(workspace, token):
            calls.append(workspace)
            return RAW_MANIFEST, None

        monkeypatch.setattr(mc_mod, "get_databricks_token", token)
        monkeypatch.setattr(mc_mod, "get_managed_config", fetch)
        assert refresh_managed_config({"workspace": selected, "profile": "developer"}).manifest == (
            normalize_managed_config(RAW_MANIFEST)
        )
        assert calls == [WORKSPACE]

    def test_foreign_warm_unmanaged_stamp_cannot_bypass_installed_workspace(self, monkeypatch):
        mc_mod._record_refresh(WORKSPACE, mc_mod.ManagedConfigResult(None, False))
        save_managed_state("https://installed.example.com", RAW_MANIFEST)
        entry = json.loads(mc_mod.REFRESH_TS_PATH.read_text())
        entry[WORKSPACE]["policy_fingerprint"] = mc_mod.managed_file_fingerprint(
            mc_mod.MANAGED_CONFIG_PATH
        )
        mc_mod.REFRESH_TS_PATH.write_text(json.dumps(entry))
        with pytest.raises(RuntimeError, match="Launch refused"):
            refresh_managed_config(_state())

    def test_explicit_configure_can_replace_the_machine_workspace(self, monkeypatch):
        save_managed_state("https://installed.example.com", RAW_MANIFEST)
        monkeypatch.setattr(mc_mod, "get_databricks_token", lambda *args: "tok")
        monkeypatch.setattr(mc_mod, "get_managed_config", lambda *args: (RAW_MANIFEST, None))
        assert refresh_managed_config(_state(), force_refresh=True).manifest == (
            normalize_managed_config(RAW_MANIFEST)
        )
        assert managed_state_workspace() == WORKSPACE


class TestManagedUpdateTime:
    def test_reads_top_level_update_time(self):
        assert managed_update_time({"update_time": "2026-09-11T16:09:31.820Z"}) == (
            "2026-09-11T16:09:31.820Z"
        )

    def test_none_when_absent_or_not_a_dict(self):
        assert managed_update_time({}) is None
        assert managed_update_time(None) is None

    def test_normalize_captures_update_time(self):
        raw = {
            "default_agent": "CODING_AGENT_CLAUDE_CODE",
            "update_time": "2026-09-11T16:09:31.820Z",
        }
        assert normalize_managed_config(raw)["update_time"] == "2026-09-11T16:09:31.820Z"


class TestManagedConfigIsNewer:
    OLDER = {"update_time": "2026-09-11T16:00:00.000Z"}
    NEWER = {"update_time": "2026-09-11T16:09:31.820Z"}

    def test_true_when_fetched_is_newer(self):
        assert managed_config_is_newer(self.NEWER, self.OLDER["update_time"]) is True

    def test_false_when_equal(self):
        assert managed_config_is_newer(self.NEWER, self.NEWER["update_time"]) is False

    def test_false_when_fetched_is_older(self):
        assert managed_config_is_newer(self.OLDER, self.NEWER["update_time"]) is False

    def test_true_when_no_prior_watermark(self):
        # First apply: nothing applied yet, so any fetched config counts as newer.
        assert managed_config_is_newer(self.NEWER, None) is True

    def test_true_when_fetched_has_no_update_time(self):
        # A config without a parseable update_time is treated as newer so a launch re-applies it
        # rather than trusting possibly-stale local settings.
        assert managed_config_is_newer({}, self.NEWER["update_time"]) is True

    def test_true_when_watermark_unparseable(self):
        assert managed_config_is_newer(self.NEWER, "not-a-timestamp") is True

    def test_handles_z_and_offset_suffixes_equivalently(self):
        assert (
            managed_config_is_newer(
                {"update_time": "2026-09-11T16:00:00+00:00"}, "2026-09-11T16:00:00Z"
            )
            is False
        )

    def test_offset_less_timestamp_does_not_raise_against_utc_watermark(self):
        # A stub value with no offset parses tz-naive; comparing it against the tz-aware persisted
        # watermark must not raise TypeError, and must still return a sane bool.
        result = managed_config_is_newer(
            {"update_time": "2024-01-15T10:30:00"}, "2024-01-15T09:00:00Z"
        )
        assert result is True
        assert (
            managed_config_is_newer({"update_time": "2024-01-15T10:30:00"}, "2024-01-15T11:00:00Z")
            is False
        )


class TestGetModelRecommendation:
    """The budget recommendation read. Every response field is optional server-side."""

    @staticmethod
    def _stub(monkeypatch, payload, reason=None):
        monkeypatch.setattr(mc_mod, "fetch_model_recommendation", lambda ws, tok: (payload, reason))

    def test_normalizes_agent_model_and_spend(self, monkeypatch):
        self._stub(
            monkeypatch,
            {
                "recommended_agent": "CODING_AGENT_OPENCODE",
                "recommended_model": "system.ai.claude-haiku-4-5",
                "current_spend": "412.50",
                "effective_threshold": "500.00",
            },
        )
        rec, reason = mc_mod.get_model_recommendation("https://w", "tok")
        assert reason is None
        assert rec == {
            "agent": "opencode",
            "model": "system.ai.claude-haiku-4-5",
            "current_spend": 412.5,
            "effective_threshold": 500.0,
        }

    def test_model_without_an_agent(self, monkeypatch):
        # A model-only tier with no default_agent recommends a model but no agent.
        self._stub(monkeypatch, {"recommended_model": "system.ai.gpt-5", "current_spend": "1.00"})
        rec, _ = mc_mod.get_model_recommendation("https://w", "tok")
        assert rec is not None and rec["agent"] is None and rec["model"] == "system.ai.gpt-5"

    def test_agent_without_a_model(self, monkeypatch):
        self._stub(monkeypatch, {"recommended_agent": "CODING_AGENT_PI", "current_spend": "1.00"})
        rec, _ = mc_mod.get_model_recommendation("https://w", "tok")
        assert rec is not None and rec["agent"] == "pi" and rec["model"] is None

    @pytest.mark.parametrize("agent_enum", ["CODING_AGENT_UNSPECIFIED", "CODING_AGENT_FUTURE", ""])
    def test_unknown_agent_is_dropped_not_fatal(self, monkeypatch, agent_enum):
        self._stub(
            monkeypatch,
            {"recommended_agent": agent_enum, "recommended_model": "m", "current_spend": "1.00"},
        )
        rec, reason = mc_mod.get_model_recommendation("https://w", "tok")
        assert reason is None
        assert rec is not None and rec["agent"] is None and rec["model"] == "m"

    def test_threshold_alone_still_reports(self, monkeypatch):
        # A budget with no spend yet still has a threshold worth showing.
        self._stub(monkeypatch, {"effective_threshold": "500.00"})
        rec, _ = mc_mod.get_model_recommendation("https://w", "tok")
        assert rec is not None and rec["effective_threshold"] == 500.0

    def test_empty_response_is_no_recommendation(self, monkeypatch):
        self._stub(monkeypatch, {})
        assert mc_mod.get_model_recommendation("https://w", "tok") == (None, None)

    def test_failed_read_surfaces_the_reason(self, monkeypatch):
        self._stub(monkeypatch, {}, reason="HTTP 500")
        assert mc_mod.get_model_recommendation("https://w", "tok") == (None, "HTTP 500")

    def test_unparseable_decimals_become_none(self, monkeypatch):
        self._stub(
            monkeypatch, {"recommended_agent": "CODING_AGENT_PI", "current_spend": "not-a-number"}
        )
        rec, _ = mc_mod.get_model_recommendation("https://w", "tok")
        assert rec is not None and rec["current_spend"] is None


class TestRevertMachinePolicy:
    def test_revert_removes_the_installed_policy(self):
        mc_mod.MANAGED_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        mc_mod.MANAGED_CONFIG_PATH.write_text('{"workspace": "x", "config": {}}', encoding="utf-8")

        assert mc_mod.revert_machine_policy() == "removed"
        assert not mc_mod.MANAGED_CONFIG_PATH.exists()

    def test_revert_without_a_policy_writes_nothing(self, monkeypatch):
        monkeypatch.setattr(
            mc_mod, "update_workspace_policy", lambda *a, **k: pytest.fail("nothing to remove")
        )

        assert mc_mod.revert_machine_policy() == "unchanged"

    @pytest.mark.parametrize("mode", ["skip_os_settings", "windows"])
    def test_revert_leaves_the_policy_where_ug_does_not_manage_it(self, monkeypatch, mode):
        mc_mod.MANAGED_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        mc_mod.MANAGED_CONFIG_PATH.write_text("{}", encoding="utf-8")
        if mode == "skip_os_settings":
            monkeypatch.setenv("UG_SKIP_OS_SETTINGS", "1")
        else:
            monkeypatch.setattr(mc_mod, "current_os", lambda: mc_mod.OS.WINDOWS)

        assert mc_mod.revert_machine_policy() == "unchanged"
        assert mc_mod.MANAGED_CONFIG_PATH.exists()

    def test_revert_without_sudo_warns_and_keeps_the_policy(self, monkeypatch):
        mc_mod.MANAGED_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        mc_mod.MANAGED_CONFIG_PATH.write_text("{}", encoding="utf-8")
        warnings = []
        monkeypatch.setattr(mc_mod, "print_warning", warnings.append)

        def denied(*_a, **_k):
            raise RuntimeError("sudo: a password is required")

        monkeypatch.setattr(mc_mod, "update_workspace_policy", denied)

        assert mc_mod.revert_machine_policy() == "failed"
        assert mc_mod.MANAGED_CONFIG_PATH.exists()
        assert "ug revert" in warnings[0]
