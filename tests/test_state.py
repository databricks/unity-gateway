"""Tests for state.py — load/save/hydrate/clear/mark_tool_managed."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

import ucode.state as state_mod
from ucode.state import (
    SELF_MANAGED_AGENTS_KEY,
    STATE_VERSION,
    add_self_managed_agent,
    build_agent_state,
    clear_state,
    get_applied_managed_update_time,
    get_model_location,
    get_provider_service,
    hydrate_state,
    is_self_managed,
    load_config_scope,
    load_full_state,
    load_state,
    mark_tool_managed,
    remove_self_managed_agent,
    save_config_scope,
    save_state,
    self_managed_agents,
    set_applied_managed_update_time,
    set_current_workspace,
    set_model_location,
    set_provider_service,
    workspace_self_managed_agents,
)

FAKE_WS = "https://example.databricks.com"
FAKE_URLS = {
    "codex": f"{FAKE_WS}/ai-gateway/codex/v1",
    "claude": f"{FAKE_WS}/ai-gateway/anthropic",
    "gemini": f"{FAKE_WS}/ai-gateway/gemini",
    "opencode": {
        "anthropic": f"{FAKE_WS}/ai-gateway/anthropic/v1",
        "gemini": f"{FAKE_WS}/ai-gateway/gemini/v1beta",
    },
    "copilot": f"{FAKE_WS}/ai-gateway/mlflow/v1",
    "pi": {
        "claude": f"{FAKE_WS}/ai-gateway/anthropic",
        "openai": f"{FAKE_WS}/ai-gateway/codex/v1",
        "gemini": f"{FAKE_WS}/ai-gateway/gemini/v1beta",
    },
}


@pytest.fixture(autouse=True)
def patch_state_path(tmp_path, monkeypatch):
    """Redirect STATE_PATH and APP_DIR to a temp directory for every test."""
    fake_state_path = tmp_path / "state.json"
    monkeypatch.setattr(state_mod, "STATE_PATH", fake_state_path)

    import ucode.config_io as config_io_mod

    monkeypatch.setattr(config_io_mod, "APP_DIR", tmp_path)


@pytest.fixture(autouse=True)
def patch_build_urls():
    """Avoid real network calls from hydrate_state."""
    with patch("ucode.state.build_shared_base_urls", return_value=FAKE_URLS):
        yield


# ---------------------------------------------------------------------------
# load_full_state
# ---------------------------------------------------------------------------


class TestLoadFullState:
    def test_returns_empty_structure_when_missing(self):
        result = load_full_state()
        assert result["state_version"] == STATE_VERSION
        assert result["current_workspace"] is None
        assert result["workspaces"] == {}

    def test_returns_empty_when_wrong_version(self, tmp_path):
        state_mod.STATE_PATH.write_text(
            json.dumps({"state_version": 0, "current_workspace": None, "workspaces": {}}),
            encoding="utf-8",
        )
        result = load_full_state()
        assert result["workspaces"] == {}

    def test_returns_empty_on_corrupt_json(self, tmp_path):
        state_mod.STATE_PATH.write_text("not json", encoding="utf-8")
        result = load_full_state()
        assert result["current_workspace"] is None

    def test_loads_valid_state(self, tmp_path):
        data = {
            "state_version": STATE_VERSION,
            "current_workspace": FAKE_WS,
            "workspaces": {FAKE_WS: {"claude_models": {"sonnet": "s4"}}},
        }
        state_mod.STATE_PATH.write_text(json.dumps(data), encoding="utf-8")
        result = load_full_state()
        assert result["current_workspace"] == FAKE_WS


# ---------------------------------------------------------------------------
# save_state / load_state round-trip
# ---------------------------------------------------------------------------


class TestSaveLoadRoundTrip:
    def test_round_trip(self):
        state = {
            "workspace": FAKE_WS,
            "claude_models": {"sonnet": "databricks-claude-sonnet-4"},
        }
        save_state(state)
        loaded = load_state()
        assert loaded["workspace"] == FAKE_WS
        assert loaded["claude_models"]["sonnet"] == "databricks-claude-sonnet-4"

    def test_persists_codex_launcher_default_in_agent_state(self):
        save_state(
            {
                "workspace": FAKE_WS,
                "codex_models": [
                    "system.ai.gpt-5",
                    "system.ai.gpt-5-1",
                    "system.ai.gpt-5-6-luna",
                ],
            }
        )

        persisted = load_full_state()["workspaces"][FAKE_WS]
        assert persisted["codex_models"][0] == "system.ai.gpt-5"
        assert "model" not in persisted["agents"]["codex"]
        assert persisted["agents"]["pi"]["model"] == "system.ai.gpt-5"

    def test_save_respects_dry_run(self):
        import ucode.config_io as config_io_mod

        config_io_mod.set_dry_run(True)
        try:
            save_state({"workspace": FAKE_WS})
            assert not state_mod.STATE_PATH.exists()
        finally:
            config_io_mod.set_dry_run(False)

    def test_load_state_returns_empty_when_no_workspace(self):
        result = load_state()
        assert result == {}


# ---------------------------------------------------------------------------
# clear_state
# ---------------------------------------------------------------------------


class TestClearState:
    def test_clears_current_workspace(self):
        save_state({"workspace": FAKE_WS, "claude_models": {}})
        clear_state()
        full = load_full_state()
        assert full["current_workspace"] is None
        assert FAKE_WS not in full.get("workspaces", {})

    def test_resets_config_scope(self):
        save_config_scope("user")
        clear_state()
        assert load_config_scope() == "global"

    def test_clear_when_no_state_is_noop(self):
        clear_state()  # should not raise


class TestProviderService:
    def test_get_returns_none_when_unset(self):
        assert get_provider_service({}, "claude") is None
        assert get_provider_service({"provider_services": {}}, "claude") is None

    def test_set_and_get_roundtrip(self):
        state = set_provider_service({}, "claude", "main.a.anthropic")
        assert state["provider_services"]["claude"] == "main.a.anthropic"
        assert get_provider_service(state, "claude") == "main.a.anthropic"
        assert get_provider_service(state, "codex") is None

    def test_set_none_clears_entry_and_key(self):
        state = set_provider_service({}, "claude", "main.a.anthropic")
        state = set_provider_service(state, "claude", None)
        assert get_provider_service(state, "claude") is None
        # Drop the empty container entirely rather than leaving {}.
        assert "provider_services" not in state

    def test_clearing_one_tool_keeps_the_other(self):
        state = set_provider_service({}, "claude", "main.a.anthropic")
        state = set_provider_service(state, "codex", "main.a.openai")
        state = set_provider_service(state, "claude", None)
        assert get_provider_service(state, "claude") is None
        assert get_provider_service(state, "codex") == "main.a.openai"


class TestModelLocation:
    def test_get_returns_none_when_unset_or_malformed(self):
        assert get_model_location({}, "claude") is None
        assert get_model_location({"model_locations": "main.models"}, "claude") is None
        assert get_model_location({"model_locations": {"claude": ""}}, "claude") is None

    def test_set_and_get_roundtrip_per_tool(self):
        state = set_model_location({}, "claude", "main.models")
        assert get_model_location(state, "claude") == "main.models"
        assert get_model_location(state, "codex") is None

    def test_set_none_clears_entry_and_key(self):
        state = set_model_location({}, "claude", "main.models")
        state = set_model_location(state, "claude", None)
        assert "model_locations" not in state

    def test_clearing_one_tool_keeps_the_other(self):
        state = set_model_location({}, "claude", "main.a")
        state = set_model_location(state, "codex", "main.b")
        state = set_model_location(state, "claude", None)
        assert get_model_location(state, "codex") == "main.b"


class TestAppliedManagedUpdateTime:
    def test_get_returns_none_when_unset(self):
        assert get_applied_managed_update_time({}) is None
        assert get_applied_managed_update_time({"applied_managed_update_time": ""}) is None

    def test_set_and_get_roundtrip(self):
        state = set_applied_managed_update_time({}, "2026-09-11T16:09:31.820Z")
        assert get_applied_managed_update_time(state) == "2026-09-11T16:09:31.820Z"

    def test_set_none_clears_the_key(self):
        state = set_applied_managed_update_time({}, "2026-09-11T16:09:31.820Z")
        state = set_applied_managed_update_time(state, None)
        assert get_applied_managed_update_time(state) is None
        assert "applied_managed_update_time" not in state

    def test_survives_save_load_roundtrip(self):
        state = {"workspace": FAKE_WS, "profile": None}
        state = set_applied_managed_update_time(state, "2026-09-11T16:09:31.820Z")
        save_state(state)
        assert get_applied_managed_update_time(load_state()) == "2026-09-11T16:09:31.820Z"


# ---------------------------------------------------------------------------
# hydrate_state
# ---------------------------------------------------------------------------


class TestHydrateState:
    def test_empty_input_returns_empty(self):
        result = hydrate_state({})
        assert result == {"managed_configs": {}, "base_urls": {}, "agents": {}}

    def test_non_dict_returns_empty(self):
        assert hydrate_state(None) == {}  # type: ignore[arg-type]
        assert hydrate_state("string") == {}  # type: ignore[arg-type]

    def test_populates_base_urls_when_workspace_present(self):
        result = hydrate_state({"workspace": FAKE_WS})
        assert result["base_urls"] == FAKE_URLS

    def test_no_base_urls_when_no_workspace(self):
        result = hydrate_state({"claude_models": {}})
        assert result["base_urls"] == {}
        assert result["agents"] == {}

    def test_populates_agent_state_when_workspace_present(self):
        result = hydrate_state(
            {
                "workspace": FAKE_WS,
                "claude_models": {"opus": "claude-opus"},
                "codex_models": ["gpt-5"],
            }
        )

        assert result["agents"]["claude"]["model"] == "claude-opus"
        assert result["agents"]["claude"]["base_url"] == FAKE_URLS["claude"]
        # Cross-platform helper, not the old POSIX `if [ -n ... ]` pipeline (#116).
        assert "auth-token" in result["agents"]["claude"]["auth_command"]
        assert "if [ -n" not in result["agents"]["claude"]["auth_command"]
        assert "model" not in result["agents"]["codex"]
        assert result["agents"]["codex"]["base_url"] == FAKE_URLS["codex"]
        # Codex runs the helper as argv (command + args), never via `sh -c`.
        codex_auth = result["agents"]["codex"]["auth"]
        assert codex_auth["command"] != "sh"
        assert codex_auth["args"][0] == "auth-token"
        assert result["agents"]["pi"]["model"] == "claude-opus"
        assert result["agents"]["pi"]["base_urls"] == FAKE_URLS["pi"]

    def test_normalizes_managed_configs_dict_entry(self):
        state = {"managed_configs": {"claude": {"keys": [["env", "X"]]}}}
        result = hydrate_state(state)
        assert result["managed_configs"]["claude"] == {"keys": [["env", "X"]]}

    def test_normalizes_managed_configs_truthy_entry(self):
        state = {"managed_configs": {"codex": True}}
        result = hydrate_state(state)
        assert result["managed_configs"]["codex"] == {"keys": []}

    def test_drops_falsy_managed_configs(self):
        state = {"managed_configs": {"codex": False, "claude": None}}
        result = hydrate_state(state)
        assert "codex" not in result["managed_configs"]
        assert "claude" not in result["managed_configs"]


class TestBuildAgentState:
    def test_returns_empty_without_workspace(self):
        result = build_agent_state({"base_urls": FAKE_URLS})
        assert result == {}

    def test_use_pat_state_builds_pat_auth_command(self):
        result = build_agent_state(
            {
                "workspace": "https://example.databricks.com",
                "profile": "DEFAULT",
                "use_pat": True,
                "base_urls": FAKE_URLS,
            }
        )
        # --use-pat threads through to the `ucode auth-token --use-pat` helper,
        # which resolves the static PAT internally on every platform.
        for agent in ("claude", "codex", "pi"):
            assert "--use-pat" in result[agent]["auth_command"]
            assert "--profile DEFAULT" in result[agent]["auth_command"]
        assert result["codex"]["auth"]["timeout_ms"] == 5000

    def test_custom_oauth_applies_to_claude_and_codex(self):
        result = build_agent_state(
            {
                "workspace": "https://example.databricks.com",
                "base_urls": FAKE_URLS,
                "custom_oauth": {
                    "client_id": "custom-client",
                    "redirect_url": "http://localhost:8020/callback",
                    "scopes": ["offline_access", "model-serving"],
                },
            }
        )

        assert "--client-id custom-client" in result["claude"]["auth_command"]
        assert result["codex"]["auth"]["args"][-1] == "offline_access,model-serving"
        assert result["codex"]["auth"]["timeout_ms"] == 180_000


# ---------------------------------------------------------------------------
# mark_tool_managed
# ---------------------------------------------------------------------------


class TestMarkToolManaged:
    def test_sets_managed_keys(self):
        state: dict = {}
        result = mark_tool_managed(state, "claude", [["env", "X"], ["apiKeyHelper"]])
        assert result["managed_configs"]["claude"] == {"keys": [["env", "X"], ["apiKeyHelper"]]}

    def test_sets_last_tool(self):
        state: dict = {}
        result = mark_tool_managed(state, "codex", [])
        assert result["last_tool"] == "codex"

    def test_preserves_existing_managed_configs(self):
        state = {"managed_configs": {"gemini": {"keys": [["GEMINI_MODEL"]]}}}
        result = mark_tool_managed(state, "codex", [["profile"]])
        assert "gemini" in result["managed_configs"]
        assert "codex" in result["managed_configs"]

    def test_records_only_keys(self):
        result = mark_tool_managed({}, "codex", [["model"]])
        assert result["managed_configs"]["codex"] == {"keys": [["model"]]}


class TestSelfManagedAgents:
    """self_managed_agents / is_self_managed / add / remove round-trip through save/load."""

    def test_round_trip(self):
        state = {"workspace": FAKE_WS}
        add_self_managed_agent(state, "opencode")
        save_state(state)
        loaded = load_state()
        assert is_self_managed(loaded, "opencode")
        assert not is_self_managed(loaded, "gemini")

    def test_remove_persists(self):
        state = {"workspace": FAKE_WS, SELF_MANAGED_AGENTS_KEY: ["opencode", "gemini"]}
        save_state(state)
        loaded = load_state()
        remove_self_managed_agent(loaded, "opencode")
        save_state(loaded)
        reloaded = load_state()
        assert not is_self_managed(reloaded, "opencode")
        assert is_self_managed(reloaded, "gemini")

    def test_key_absent_after_all_removed(self):
        state = {"workspace": FAKE_WS, SELF_MANAGED_AGENTS_KEY: ["opencode"]}
        save_state(state)
        loaded = load_state()
        remove_self_managed_agent(loaded, "opencode")
        save_state(loaded)
        reloaded = load_state()
        assert self_managed_agents(reloaded) == []
        # key absent from the raw state too (not persisted as empty list)
        full = load_full_state()
        ws_raw = full["workspaces"].get(FAKE_WS, {})
        assert SELF_MANAGED_AGENTS_KEY not in ws_raw


class TestSelfManagedWorkspaceIsolation:
    """Each workspace's self-managed opt-in persists on its own block across switches.

    The configure-switch logic that reads the destination's own list lives in
    ``TestConfigureSharedStateWorkspaceIsolation`` (tests/test_agents_commands.py); this
    exercises the underlying persistence that keeps the lists independent.
    """

    def test_a_to_b_to_a_round_trip_and_removal_stay_isolated(self):
        a = "https://a.databricks.com"
        b = "https://b.databricks.com"

        # In A, opt into OpenCode.
        save_state({"workspace": a, SELF_MANAGED_AGENTS_KEY: ["opencode"]})

        # Switch to B: it has its own (empty) list, not A's; opt into a different agent there.
        set_current_workspace(b)
        assert self_managed_agents(load_state()) == []
        save_state({"workspace": b, SELF_MANAGED_AGENTS_KEY: ["copilot"]})

        # Switch back to A: A still carries only its own opt-in.
        set_current_workspace(a)
        assert self_managed_agents(load_state()) == ["opencode"]

        # Removing the opt-in in A leaves B's list untouched.
        state = load_state()
        remove_self_managed_agent(state, "opencode")
        save_state(state)
        assert workspace_self_managed_agents(a) == []
        assert workspace_self_managed_agents(b) == ["copilot"]


# ---------------------------------------------------------------------------
# Hosts that serve several workspaces
# ---------------------------------------------------------------------------

SHARED_WS = "https://acme.cloud.databricks.com"
KEY_A = f"{SHARED_WS}?w=111"
KEY_B = f"{SHARED_WS}?w=222"


def _raw_state() -> dict:
    return json.loads(state_mod.STATE_PATH.read_text(encoding="utf-8"))


class TestClassicStateUnchanged:
    def test_classic_round_trip_writes_only_the_bare_host(self):
        save_state({"workspace": FAKE_WS, "profile": "p"})
        assert "workspace_id" not in load_state()
        raw = _raw_state()
        assert set(raw) == {"state_version", "current_workspace", "workspaces"}
        assert raw["current_workspace"] == FAKE_WS
        assert list(raw["workspaces"]) == [FAKE_WS]
        block = raw["workspaces"][FAKE_WS]
        assert "workspace_id" not in block
        assert state_mod.ALIAS_OF_KEY not in block


class TestMultiWorkspaceHostState:
    def test_save_keys_by_workspace_and_mirrors_the_bare_host(self):
        save_state({"workspace": SHARED_WS, "workspace_id": "111", "profile": "acme"})
        raw = _raw_state()
        assert raw["current_workspace"] == SHARED_WS
        assert raw[state_mod.CURRENT_WORKSPACE_KEY] == KEY_A
        assert raw["workspaces"][KEY_A]["workspace_id"] == "111"
        alias = raw["workspaces"][SHARED_WS]
        assert alias[state_mod.ALIAS_OF_KEY] == KEY_A
        assert alias["profile"] == "acme"
        assert alias["workspace_id"] == "111"

    def test_load_returns_bare_host_and_id(self):
        save_state({"workspace": SHARED_WS, "workspace_id": "111"})
        loaded = load_state()
        assert loaded["workspace"] == SHARED_WS
        assert loaded["workspace_id"] == "111"
        assert state_mod.ALIAS_OF_KEY not in loaded
        assert loaded["base_urls"] == FAKE_URLS

    def test_alias_follows_the_current_workspace(self):
        save_state({"workspace": SHARED_WS, "workspace_id": "111", "profile": "a"})
        save_state({"workspace": SHARED_WS, "workspace_id": "222", "profile": "b"})
        raw = _raw_state()
        assert set(raw["workspaces"]) == {KEY_A, KEY_B, SHARED_WS}
        assert raw["workspaces"][SHARED_WS][state_mod.ALIAS_OF_KEY] == KEY_B
        set_current_workspace(KEY_A)
        raw = _raw_state()
        assert raw[state_mod.CURRENT_WORKSPACE_KEY] == KEY_A
        assert raw["workspaces"][SHARED_WS]["profile"] == "a"
        assert load_state()["workspace_id"] == "111"

    def test_switching_to_a_classic_workspace_drops_the_key(self):
        save_state({"workspace": SHARED_WS, "workspace_id": "111"})
        save_state({"workspace": FAKE_WS})
        raw = _raw_state()
        assert raw["current_workspace"] == FAKE_WS
        assert state_mod.CURRENT_WORKSPACE_KEY not in raw
        assert "workspace_id" not in load_state()

    def test_stale_key_from_an_older_version_is_ignored(self):
        save_state({"workspace": SHARED_WS, "workspace_id": "111"})
        raw = _raw_state()
        raw["current_workspace"] = FAKE_WS
        raw["workspaces"][FAKE_WS] = {"profile": "classic"}
        state_mod.STATE_PATH.write_text(json.dumps(raw), encoding="utf-8")
        loaded = load_state()
        assert loaded["workspace"] == FAKE_WS
        assert loaded["profile"] == "classic"

    def test_clear_removes_the_block_and_its_alias(self):
        save_state({"workspace": SHARED_WS, "workspace_id": "111"})
        clear_state()
        raw = _raw_state()
        assert raw["workspaces"] == {}
        assert raw["current_workspace"] is None
        assert state_mod.CURRENT_WORKSPACE_KEY not in raw

    def test_first_selection_inherits_a_bare_host_self_managed_list(self):
        state_mod.STATE_PATH.write_text(
            json.dumps(
                {
                    "state_version": STATE_VERSION,
                    "current_workspace": SHARED_WS,
                    "workspaces": {SHARED_WS: {SELF_MANAGED_AGENTS_KEY: ["gemini"]}},
                }
            ),
            encoding="utf-8",
        )
        assert workspace_self_managed_agents(KEY_A) == ["gemini"]
        save_state(
            {"workspace": SHARED_WS, "workspace_id": "111", SELF_MANAGED_AGENTS_KEY: ["gemini"]}
        )
        save_state({**load_state(), "profile": "p"})
        assert _raw_state()["workspaces"][KEY_A][SELF_MANAGED_AGENTS_KEY] == ["gemini"]

    def test_selecting_the_bare_host_selects_its_current_workspace(self):
        save_state({"workspace": SHARED_WS, "workspace_id": "111"})
        save_state({"workspace": FAKE_WS})
        set_current_workspace(SHARED_WS)
        loaded = load_state()
        assert (loaded["workspace"], loaded["workspace_id"]) == (SHARED_WS, "111")

    def test_older_version_edits_to_the_alias_are_not_read(self):
        """An older ug can't select a workspace on such a host; its edits to the copy don't apply."""
        save_state({"workspace": SHARED_WS, "workspace_id": "111", "profile": "a"})
        raw = _raw_state()
        raw["workspaces"][SHARED_WS]["profile"] = "edited-by-older-ug"
        state_mod.STATE_PATH.write_text(json.dumps(raw), encoding="utf-8")
        assert load_state()["profile"] == "a"

    def test_alias_has_no_self_managed_list_of_its_own(self):
        save_state({"workspace": SHARED_WS, "workspace_id": "111", SELF_MANAGED_AGENTS_KEY: ["pi"]})
        assert workspace_self_managed_agents(KEY_A) == ["pi"]
        assert workspace_self_managed_agents(SHARED_WS) == []


class TestLegacyWorkspaceKeys:
    def _write(self, workspaces: dict, current: str) -> None:
        state_mod.STATE_PATH.write_text(
            json.dumps(
                {
                    "state_version": STATE_VERSION,
                    "current_workspace": current,
                    "workspaces": workspaces,
                }
            ),
            encoding="utf-8",
        )

    def test_pasted_query_is_rekeyed_to_the_bare_host(self):
        legacy = f"{FAKE_WS}/?o=123"
        self._write({legacy: {"workspace": legacy, "profile": "p"}}, legacy)
        loaded = load_state()
        assert loaded["workspace"] == FAKE_WS
        assert loaded["profile"] == "p"
        save_state(loaded)
        assert list(_raw_state()["workspaces"]) == [FAKE_WS]

    def test_existing_bare_host_block_wins(self):
        legacy = f"{FAKE_WS}/browse"
        self._write({legacy: {"profile": "old"}, FAKE_WS: {"profile": "new"}}, FAKE_WS)
        assert load_state()["profile"] == "new"
        assert set(load_full_state()["workspaces"]) == {legacy, FAKE_WS}

    def test_canonical_keys_are_untouched(self):
        self._write({KEY_A: {"workspace_id": "111"}, FAKE_WS: {"profile": "c"}}, FAKE_WS)
        assert set(load_full_state()["workspaces"]) == {KEY_A, FAKE_WS}

    def test_selector_key_saved_by_an_older_version_is_rekeyed(self):
        legacy = f"{FAKE_WS}?w=1"
        self._write({legacy: {"workspace": legacy, "profile": "p"}}, legacy)
        loaded = load_state()
        assert loaded["workspace"] == FAKE_WS
        assert "workspace_id" not in loaded
