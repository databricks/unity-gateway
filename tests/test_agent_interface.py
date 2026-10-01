"""Conformance tests for the Agent interface and the AGENTS registry.

Every agent ug drives has to satisfy `agents/interface.py` the same way, so these tests are
parametrized over the registry rather than written per agent. An agent that grows its own
native class (replacing `LegacyAgent`) must keep passing them unchanged.
"""

from __future__ import annotations

import copy

import pytest

from ucode.agents import AGENTS, TOOL_SPECS, configure_tool
from ucode.agents.interface import Agent, McpClient, Models
from ucode.mcp import MCP_CLIENTS

# One state that gives every agent something to find, so `models()` is exercised with a
# populated inventory and not only the empty case.
POPULATED_STATE: dict = {
    "workspace": "https://ws.databricks.com",
    "claude_models": {"opus": "claude-opus", "sonnet": "claude-sonnet"},
    "codex_models": ["codex-mini"],
    "gemini_models": ["gemini-3"],
    "opencode_models": {"anthropic": ["claude-sonnet"], "gemini": ["gemini-3"]},
}


@pytest.fixture
def no_revert_side_effects(monkeypatch):
    """Stub the file mutations `revert()` performs.

    `SPEC` config/backup paths and the OS-managed settings files point at the developer's real
    machine, so conformance must check the shape of the rows without touching anything.
    """
    from ucode.agents import claude, codex, copilot, gemini, legacy, opencode, pi

    monkeypatch.setattr(legacy, "restore_file", lambda *_a: False)
    monkeypatch.setattr(codex, "restore_file", lambda *_a: False)
    monkeypatch.setattr(claude, "restore_file", lambda *_a: False)
    monkeypatch.setattr(gemini, "restore_file", lambda *_a: False)
    monkeypatch.setattr(opencode, "restore_file", lambda *_a: False)
    monkeypatch.setattr(copilot, "restore_file", lambda *_a: False)
    monkeypatch.setattr(pi, "restore_file", lambda *_a: False)
    monkeypatch.setattr(codex, "revert_legacy_shared_config", lambda: True)
    monkeypatch.setattr(codex, "revert_managed_config", lambda: "unchanged")
    monkeypatch.setattr(claude, "revert_managed_settings", lambda: "unchanged")


class TestRegistry:
    def test_lists_every_agent_in_order(self):
        assert list(AGENTS) == ["codex", "claude", "gemini", "opencode", "copilot", "pi"]

    def test_matches_the_derived_tool_specs(self):
        assert list(AGENTS) == list(TOOL_SPECS)


@pytest.mark.parametrize("tool", list(AGENTS))
class TestAgentConformance:
    def test_satisfies_the_protocol(self, tool):
        assert isinstance(AGENTS[tool], Agent)

    def test_describes_itself_and_its_install(self, tool):
        agent = AGENTS[tool]
        assert agent.display
        assert agent.install.binary
        assert agent.install.package

    def test_declares_whether_it_takes_mcp_servers(self, tool):
        # `mcp` has no default, so every agent states this outright rather than inheriting "no".
        mcp = AGENTS[tool].mcp
        assert mcp is None or isinstance(mcp, McpClient)

    @pytest.mark.parametrize("state", [{}, POPULATED_STATE], ids=["empty", "populated"])
    def test_models_returns_models_without_mutating_state(self, tool, state):
        given = copy.deepcopy(state)
        models = AGENTS[tool].models(given)
        assert isinstance(models, Models)
        assert all(isinstance(model, str) and model for model in models.available)
        assert models.default is None or models.default in models.available
        assert given == state

    def test_models_finds_the_populated_inventory(self, tool):
        # Every agent routes at least one of the discovered families, so none of them may come
        # back empty for a workspace that serves claude, codex and gemini.
        assert AGENTS[tool].models(POPULATED_STATE).available

    def test_revert_returns_label_outcome_rows(self, tool, no_revert_side_effects):
        rows = AGENTS[tool].revert({})
        assert rows
        for row in rows:
            assert isinstance(row, tuple) and len(row) == 2
            label, outcome = row
            assert isinstance(label, str) and label
            assert isinstance(outcome, str) and outcome


class TestMcpClientRegistry:
    """The `McpClient` half of the contract: ug's MCP paths dispatch only through these objects."""

    def test_lists_every_mcp_capable_agent_then_the_mcp_only_clients(self):
        assert list(MCP_CLIENTS) == ["codex", "claude", "gemini", "opencode", "copilot", "cursor"]

    @pytest.mark.parametrize("client", list(MCP_CLIENTS))
    def test_satisfies_the_protocol(self, client):
        target = MCP_CLIENTS[client]
        assert isinstance(target, McpClient)
        assert target.display and target.binary
        # None means "always use the stdio proxy"; a string must be a real published app id.
        assert target.oauth_client_id is None or target.oauth_client_id

    def test_each_agents_client_is_the_registered_one(self):
        for tool, agent in AGENTS.items():
            assert agent.mcp is MCP_CLIENTS.get(tool)

    def test_pi_takes_no_mcp_servers(self):
        # Pi has no MCP support today, so it has no client and never appears in the registry.
        assert AGENTS["pi"].mcp is None
        assert "pi" not in MCP_CLIENTS

    def test_cursor_is_an_mcp_client_but_not_an_agent(self):
        # Cursor runs models on the user's own account, so ug configures none for it.
        assert isinstance(MCP_CLIENTS["cursor"], McpClient)
        assert "cursor" not in AGENTS


class TestModelsSemantics:
    """The status/availability semantics every agent's `models()` has to keep."""

    @pytest.mark.parametrize("tool", ["claude", "codex"])
    def test_claude_and_codex_leave_the_starting_model_to_the_agent(self, tool):
        assert AGENTS[tool].models(POPULATED_STATE).default is None

    @pytest.mark.parametrize("tool", ["gemini", "opencode", "copilot", "pi"])
    def test_other_agents_pin_the_first_available_model(self, tool):
        models = AGENTS[tool].models(POPULATED_STATE)
        assert models.default == models.available[0]

    @pytest.mark.parametrize("tool", list(AGENTS))
    def test_pinned_default_model_wins(self, tool):
        state = {**POPULATED_STATE, f"{tool}_default_model": "pinned-model"}
        assert AGENTS[tool].models(state).default == "pinned-model"

    @pytest.mark.parametrize("tool", ["claude", "codex"])
    def test_static_model_list_replaces_discovery(self, tool):
        state = {**POPULATED_STATE, f"{tool}_static_models": ["only-this"]}
        assert AGENTS[tool].models(state).available == ("only-this",)

    def test_copilot_falls_back_to_claude_then_codex(self):
        assert AGENTS["copilot"].models(POPULATED_STATE).available == (
            "claude-opus",
            "claude-sonnet",
            "codex-mini",
        )

    def test_pi_falls_back_to_claude_codex_and_gemini(self):
        assert AGENTS["pi"].models(POPULATED_STATE).available == (
            "claude-opus",
            "claude-sonnet",
            "codex-mini",
            "gemini-3",
        )

    def test_duplicates_are_collapsed(self):
        # copilot composes the claude and codex lists, which can name the same id in both.
        state = {"claude_models": {"opus": "m", "sonnet": "other"}, "codex_models": ["m"]}
        assert AGENTS["copilot"].models(state).available == ("m", "other")


class TestConfigureUnknownTool:
    def test_unknown_tool_raises_instead_of_configuring_opencode(self, monkeypatch):
        # The pre-registry dispatcher ended in an `else` that wrote OpenCode's config, so a
        # typo'd agent name silently configured OpenCode. The registry lookup must raise.
        from ucode.agents import opencode

        monkeypatch.setattr(
            opencode,
            "write_tool_config",
            lambda *_a, **_k: pytest.fail("unknown tool must not configure OpenCode"),
        )
        with pytest.raises(KeyError):
            configure_tool("not-an-agent", {"workspace": "https://ws.databricks.com"}, "model")
