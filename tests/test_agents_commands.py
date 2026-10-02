"""Tests for `ug agents` commands and self-managed-agent state helpers."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from typer.testing import CliRunner

import ucode.cli as cli_mod
from ucode.cli import app
from ucode.state import (
    SELF_MANAGED_AGENTS_KEY,
    add_self_managed_agent,
    is_self_managed,
    remove_self_managed_agent,
    self_managed_agents,
)

runner = CliRunner()

WORKSPACE = "https://example.databricks.com"
BASE_STATE = {"workspace": WORKSPACE, "managed_configs": {}, "available_tools": ["claude"]}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_state_writes():
    """Prevent tests from touching the real state file."""
    with (
        patch("ucode.state.save_state"),
        patch("ucode.cli.save_state"),
        patch("ucode.agents.__init__.save_state"),
    ):
        yield


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------


class TestSelfManagedAgentsHelpers:
    def test_empty_when_key_absent(self):
        assert self_managed_agents({}) == []

    def test_returns_list_copy(self):
        state = {SELF_MANAGED_AGENTS_KEY: ["opencode"]}
        result = self_managed_agents(state)
        result.append("gemini")
        assert state[SELF_MANAGED_AGENTS_KEY] == ["opencode"]

    def test_is_self_managed_false_when_absent(self):
        assert not is_self_managed({}, "opencode")

    def test_is_self_managed_true_when_present(self):
        state = {SELF_MANAGED_AGENTS_KEY: ["opencode"]}
        assert is_self_managed(state, "opencode")

    def test_add_appends_tool(self):
        state: dict = {}
        add_self_managed_agent(state, "opencode")
        assert state[SELF_MANAGED_AGENTS_KEY] == ["opencode"]

    def test_add_is_idempotent(self):
        state = {SELF_MANAGED_AGENTS_KEY: ["opencode"]}
        add_self_managed_agent(state, "opencode")
        assert state[SELF_MANAGED_AGENTS_KEY] == ["opencode"]

    def test_remove_drops_tool(self):
        state = {SELF_MANAGED_AGENTS_KEY: ["opencode", "gemini"]}
        remove_self_managed_agent(state, "opencode")
        assert state[SELF_MANAGED_AGENTS_KEY] == ["gemini"]

    def test_remove_drops_key_when_list_becomes_empty(self):
        state = {SELF_MANAGED_AGENTS_KEY: ["opencode"]}
        remove_self_managed_agent(state, "opencode")
        assert SELF_MANAGED_AGENTS_KEY not in state

    def test_remove_is_noop_when_absent(self):
        state: dict = {}
        remove_self_managed_agent(state, "opencode")
        assert state == {}

    def test_self_managed_survives_state_mutation(self):
        """add/remove mutate the dict in place so the caller's save_state sees the change."""
        state: dict = {"workspace": WORKSPACE}
        add_self_managed_agent(state, "opencode")
        assert is_self_managed(state, "opencode")
        remove_self_managed_agent(state, "opencode")
        assert not is_self_managed(state, "opencode")
        assert SELF_MANAGED_AGENTS_KEY not in state


# ---------------------------------------------------------------------------
# `ug agents add`
# ---------------------------------------------------------------------------


class TestAgentsAdd:
    @staticmethod
    def _run(args, *, managed=None, state=None):
        state = dict(BASE_STATE) if state is None else state
        with (
            patch("ucode.cli.load_state", return_value=state),
            patch("ucode.cli._fetch_managed_config", return_value=(managed, False)),
            patch("ucode.cli.managed_write_session"),
            patch("ucode.cli._setup_single_agent") as setup_spy,
            patch("ucode.cli.add_self_managed_agent", wraps=add_self_managed_agent) as add_spy,
            patch("ucode.cli.save_state") as save_spy,
        ):
            result = runner.invoke(app, ["agents", "add", *args])
        return result, setup_spy, add_spy, save_spy

    def test_sets_up_and_records(self):
        managed = {"enabled_agents": {"claude": {}}}
        result, setup_spy, add_spy, save_spy = self._run(["opencode"], managed=managed)
        assert result.exit_code == 0, result.output
        assert setup_spy.called  # runs the agent setup flow at add time
        assert setup_spy.call_args.kwargs["apply_managed"] is False  # standalone, not admin config
        assert add_spy.called
        assert save_spy.called
        assert "self-managed" in result.output.lower() or "Added" in result.output

    def test_idempotent_when_already_self_managed(self):
        state = {**BASE_STATE, SELF_MANAGED_AGENTS_KEY: ["opencode"]}
        managed = {"enabled_agents": {"claude": {}}}
        result, setup_spy, add_spy, _ = self._run(["opencode"], managed=managed, state=state)
        assert result.exit_code == 0
        assert not setup_spy.called  # no reconfigure flow yet
        assert not add_spy.called
        assert "already" in result.output

    def test_noop_when_admin_managed(self):
        managed = {"enabled_agents": {"opencode": {}}}
        result, setup_spy, add_spy, _ = self._run(["opencode"], managed=managed)
        assert result.exit_code == 0
        assert not setup_spy.called
        assert not add_spy.called
        assert "admin" in result.output.lower()

    def test_sets_up_and_records_when_no_managed_config(self):
        result, setup_spy, add_spy, save_spy = self._run(["opencode"], managed=None)
        assert result.exit_code == 0
        assert setup_spy.called
        assert add_spy.called
        assert save_spy.called

    def test_unknown_agent_exits_1(self):
        result, *_ = self._run(["notanagent"])
        assert result.exit_code == 1
        assert "Unknown agent" in result.output

    def test_no_workspace_exits_1(self):
        result, setup_spy, *_ = self._run(["opencode"], state={})
        assert result.exit_code == 1
        assert "ug configure" in result.output
        assert not setup_spy.called

    def test_all_valid_agent_names_accepted(self):
        managed = {"enabled_agents": {"claude": {}}}
        for tool in ("codex", "gemini", "copilot", "pi", "opencode"):
            result, setup_spy, add_spy, _ = self._run([tool], managed=managed)
            assert result.exit_code == 0, f"{tool}: {result.output}"
            assert setup_spy.called
            assert add_spy.called


# ---------------------------------------------------------------------------
# `ug agents remove`
# ---------------------------------------------------------------------------


class TestAgentsRemove:
    @staticmethod
    def _run(args, *, managed=None, state=None):
        state = dict(BASE_STATE) if state is None else state
        with (
            patch("ucode.cli.load_state", return_value=state),
            patch("ucode.cli._fetch_managed_config", return_value=(managed, False)),
            patch("ucode.cli.remove_self_managed_agent", wraps=remove_self_managed_agent) as rm_spy,
            patch("ucode.cli.save_state") as save_spy,
        ):
            result = runner.invoke(app, ["agents", "remove", *args])
        return result, rm_spy, save_spy

    def test_removes_existing_self_managed_agent(self):
        state = {**BASE_STATE, SELF_MANAGED_AGENTS_KEY: ["opencode"]}
        managed = {"enabled_agents": {"claude": {}}}
        result, rm_spy, save_spy = self._run(["opencode"], managed=managed, state=state)
        assert result.exit_code == 0, result.output
        assert rm_spy.called
        assert save_spy.called
        assert "Removed" in result.output

    def test_noop_when_not_in_list(self):
        managed = {"enabled_agents": {"claude": {}}}
        result, rm_spy, save_spy = self._run(["opencode"], managed=managed)
        assert result.exit_code == 0
        assert not rm_spy.called
        assert not save_spy.called
        assert "not in" in result.output.lower()

    def test_error_when_admin_managed(self):
        managed = {"enabled_agents": {"opencode": {}}}
        result, rm_spy, save_spy = self._run(["opencode"], managed=managed)
        assert result.exit_code == 1
        assert not rm_spy.called
        assert not save_spy.called
        assert "admin" in result.output.lower()

    def test_unknown_agent_exits_1(self):
        result, _, _ = self._run(["notanagent"])
        assert result.exit_code == 1

    def test_no_workspace_exits_1(self):
        result, _, _ = self._run(["opencode"], state={})
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# `ug agents list`
# ---------------------------------------------------------------------------


class TestAgentsList:
    @staticmethod
    def _run(*, managed=None, state=None):
        state = dict(BASE_STATE) if state is None else state
        with (
            patch("ucode.cli.load_state", return_value=state),
            patch("ucode.cli._fetch_managed_config", return_value=(managed, False)),
        ):
            result = runner.invoke(app, ["agents", "list"])
        return result

    def test_shows_agent_table(self):
        managed = {"enabled_agents": {"claude": {}}}
        result = self._run(managed=managed)
        assert result.exit_code == 0, result.output
        assert "Claude Code" in result.output

    def test_labels_admin_managed(self):
        managed = {"enabled_agents": {"claude": {}}}
        result = self._run(managed=managed)
        assert "admin-managed" in result.output

    def test_labels_self_managed(self):
        state = {**BASE_STATE, SELF_MANAGED_AGENTS_KEY: ["opencode"]}
        managed = {"enabled_agents": {"claude": {}}}
        result = self._run(managed=managed, state=state)
        assert "self-managed" in result.output
        assert "OpenCode" in result.output

    def test_omits_agents_the_managed_config_does_not_enable(self):
        # In a managed workspace, an agent that is neither admin-enabled nor self-managed is hidden
        # (no "not enabled" row); `ug agents add` surfaces it.
        state = {**BASE_STATE, SELF_MANAGED_AGENTS_KEY: ["opencode"]}
        managed = {"enabled_agents": {"claude": {}}}
        result = self._run(managed=managed, state=state)
        assert result.exit_code == 0, result.output
        assert "not enabled" not in result.output
        assert "Claude Code" in result.output  # admin-managed, shown
        assert "OpenCode" in result.output  # self-managed, shown
        assert "Gemini" not in result.output  # neither, omitted

    def test_self_managed_agent_disappears_after_removal(self):
        managed = {"enabled_agents": {"claude": {}}}
        with_opencode = self._run(
            managed=managed, state={**BASE_STATE, SELF_MANAGED_AGENTS_KEY: ["opencode"]}
        )
        assert "OpenCode" in with_opencode.output
        without_opencode = self._run(managed=managed, state=dict(BASE_STATE))
        assert "OpenCode" not in without_opencode.output

    def test_no_managed_config_says_all_available(self):
        result = self._run(managed=None)
        assert result.exit_code == 0
        assert "available" in result.output.lower() or "No managed config" in result.output

    def test_no_workspace_exits_1(self):
        result = self._run(state={})
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Launch gate: _reject_disabled_agent
# ---------------------------------------------------------------------------


class TestRejectDisabledAgentHint:
    """The error now tells the developer how to add the agent self-managed."""

    @staticmethod
    def _reject(managed, tool):
        cli_mod._reject_disabled_agent(managed, tool)

    def test_hint_in_error_message(self):
        managed = {"enabled_agents": {"claude": {}}}
        with pytest.raises(RuntimeError, match="ug agents add opencode"):
            self._reject(managed, "opencode")

    def test_admin_enabled_tool_not_blocked(self):
        self._reject({"enabled_agents": {"claude": {}}}, "claude")

    def test_no_managed_config_not_blocked(self):
        self._reject(None, "opencode")


# ---------------------------------------------------------------------------
# Launch gate: self-managed tools bypass the gate and run unmanaged
# ---------------------------------------------------------------------------


class TestSelfManagedLaunchGate:
    """A self-managed tool passes the gate and gets managed=None so no admin settings apply."""

    MANAGED_WITH_CLAUDE = {"enabled_agents": {"claude": {}}}

    @staticmethod
    def _run_launch(tool, *, state, managed_return):
        with (
            patch("ucode.cli.load_state", return_value=state),
            patch("ucode.cli.apply_pat_environment"),
            patch("ucode.cli.ensure_bootstrap_dependencies"),
            patch("ucode.cli.ensure_provider_state", return_value=state),
            patch("ucode.cli._migrate_legacy_smart_routing", return_value=state),
            patch("ucode.cli._fetch_managed_config", return_value=(managed_return, False)),
            patch("ucode.cli.configure_shared_state", return_value=state),
            patch("ucode.cli.configure_tool", return_value=state),
            patch("ucode.cli.launch_agent"),
            patch("ucode.cli._fetch_budget_recommendation", return_value=None),
            patch("ucode.cli.resolve_state", return_value=state) as resolve_state_spy,
            patch("ucode.cli.resolve_launch_model", return_value=(state, None)),
        ):
            result = runner.invoke(app, [tool])
        return result, resolve_state_spy

    def test_self_managed_tool_is_allowed(self):
        state = {
            "workspace": WORKSPACE,
            "base_urls": {"opencode": f"{WORKSPACE}/ai-gateway/opencode"},
            "managed_configs": {},
            "available_tools": ["opencode"],
            SELF_MANAGED_AGENTS_KEY: ["opencode"],
        }
        result, resolve_spy = self._run_launch(
            "opencode", state=state, managed_return=self.MANAGED_WITH_CLAUDE
        )
        assert result.exit_code == 0, result.output
        assert "doesn't enable" not in result.output
        # Self-managed: managed config nulled, so resolve_state never called.
        resolve_spy.assert_not_called()

    def test_self_managed_launch_prints_self_managed_note(self):
        state = {
            "workspace": WORKSPACE,
            "base_urls": {"opencode": f"{WORKSPACE}/ai-gateway/opencode"},
            "managed_configs": {},
            "available_tools": ["opencode"],
            SELF_MANAGED_AGENTS_KEY: ["opencode"],
        }
        result, _ = self._run_launch(
            "opencode", state=state, managed_return=self.MANAGED_WITH_CLAUDE
        )
        assert result.exit_code == 0, result.output
        assert "self-managed" in result.output
        assert "No managed coding agent config found" not in result.output

    def test_non_self_managed_still_blocked(self):
        state = {
            "workspace": WORKSPACE,
            "base_urls": {},
            "managed_configs": {},
            "available_tools": ["opencode"],
        }
        result, _ = self._run_launch(
            "opencode", state=state, managed_return=self.MANAGED_WITH_CLAUDE
        )
        assert result.exit_code == 1
        assert "doesn't enable" in result.output

    def test_admin_enabled_agent_applies_managed_config(self):
        """Admin-enabled tool: managed config is applied even if the developer also added it."""
        state = {
            "workspace": WORKSPACE,
            "base_urls": {"claude": f"{WORKSPACE}/ai-gateway/anthropic"},
            "managed_configs": {},
            "available_tools": ["claude"],
            "claude_models": {"sonnet": "databricks-claude-sonnet-4"},
            SELF_MANAGED_AGENTS_KEY: ["claude"],
        }
        managed_with_claude = {"enabled_agents": {"claude": {}}}
        result, resolve_spy = self._run_launch(
            "claude", state=state, managed_return=managed_with_claude
        )
        assert result.exit_code == 0, result.output
        # resolve_state called proves managed config was applied.
        resolve_spy.assert_called_once()


class TestConfigureSharedStateWorkspaceIsolation:
    """The self-managed opt-in stays scoped to its own workspace across configures."""

    @staticmethod
    def _configure(monkeypatch, *, source_list, dest_list):
        source = "https://a.databricks.com"
        dest = "https://b.databricks.com"
        monkeypatch.setattr(
            "ucode.cli.load_state",
            lambda: {"workspace": source, SELF_MANAGED_AGENTS_KEY: list(source_list)},
        )
        # workspace_self_managed_agents reads the destination workspace's own persisted list.
        monkeypatch.setattr("ucode.cli.workspace_self_managed_agents", lambda ws: list(dest_list))
        monkeypatch.setattr("ucode.cli.normalize_workspace_url", lambda ws: ws)
        monkeypatch.setattr("ucode.cli.build_shared_base_urls", lambda ws: {})
        monkeypatch.setattr("ucode.cli.find_profile_name_for_host", lambda ws: None)
        monkeypatch.setattr("ucode.cli.purge_cross_workspace_mcp_residue", lambda *a, **kw: None)
        monkeypatch.setattr("ucode.cli.save_state", lambda s: None)
        # skip_preflight returns right after the base state is assembled — no login/discovery.
        return cli_mod.configure_shared_state(dest, skip_preflight=True)

    def test_destination_does_not_inherit_source_list(self, monkeypatch):
        state = self._configure(monkeypatch, source_list=["opencode"], dest_list=[])
        assert self_managed_agents(state) == []

    def test_destination_keeps_its_own_list(self, monkeypatch):
        state = self._configure(monkeypatch, source_list=["opencode"], dest_list=["copilot"])
        assert self_managed_agents(state) == ["copilot"]
