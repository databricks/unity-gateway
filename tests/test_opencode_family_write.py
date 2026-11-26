"""Tests for how OpenCode and Kilo write their config file and MCP table.

They pin write_tool_config and the MCP writers on both agents before the shared code moves."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from ucode.agents import kilo, opencode
from ucode.databricks import build_opencode_base_urls

WS = "https://example.databricks.com"
ARGV = ["/bin/ug", "mcp-proxy", "--url", f"{WS}/api/2.0/mcp/functions/system/ai"]

# (module, tool name in state, config file name)
AGENTS = [
    pytest.param(opencode, "opencode", "opencode.json", id="opencode"),
    pytest.param(kilo, "kilo", "kilo.json", id="kilo"),
]


@pytest.fixture
def paths(request, tmp_path, monkeypatch):
    module = request.node.callspec.params["agent"]
    name = "OPENCODE" if module is opencode else "KILO"
    config = tmp_path / "config" / f"{module.SPEC['binary']}.json"
    backup = tmp_path / "backup.json"
    monkeypatch.setattr(module, f"{name}_CONFIG_PATH", config)
    monkeypatch.setattr(module, f"{name}_BACKUP_PATH", backup)
    monkeypatch.setattr("ucode.config_io.APP_DIR", tmp_path)
    config.parent.mkdir(parents=True)
    return config, backup


def _state(**extra) -> dict:
    state = {
        "workspace": WS,
        "base_urls": {"opencode": {"anthropic": f"{WS}/ai-gateway/anthropic/v1"}},
        "opencode_models": {"anthropic": ["claude-sonnet"]},
        "managed_configs": {},
    }
    state.update(extra)
    return state


def _write(module, state, model="claude-sonnet", token="tok"):
    with (
        patch.object(module, "agent_version", return_value="9.9.9"),
        patch.object(module, "save_state") as save,
    ):
        result = module.write_tool_config(state, model, token=token)
    return result, save


@pytest.mark.parametrize(("agent", "tool", "_file"), AGENTS)
class TestWriteToolConfigContract:
    def test_returns_state_and_token_and_saves_once(self, agent, tool, _file, paths):
        (state, token), save = _write(agent, _state())

        assert token == "tok"
        save.assert_called_once()
        assert save.call_args.args == (state,)

    def test_marks_the_tool_managed_with_the_overlay_keys(self, agent, tool, _file, paths):
        (state, _), _save = _write(agent, _state())

        assert state["managed_configs"][tool] == {
            "keys": [["model"], ["provider", "databricks-anthropic"]]
        }
        assert state["last_tool"] == tool
        assert list(state["managed_configs"]) == [tool]

    def test_keeps_other_tools_managed_entries(self, agent, tool, _file, paths):
        state = _state(managed_configs={"claude": {"keys": [["env"]]}})

        (state, _), _save = _write(agent, state)

        assert state["managed_configs"]["claude"] == {"keys": [["env"]]}
        assert state["managed_configs"][tool]["keys"][0] == ["model"]

    def test_fetches_a_token_with_workspace_and_profile_when_none_is_given(
        self, agent, tool, _file, paths
    ):
        with (
            patch.object(agent, "agent_version", return_value="9.9.9"),
            patch.object(agent, "save_state"),
            patch.object(agent, "get_databricks_token", return_value="fetched") as fetch,
        ):
            _, token = agent.write_tool_config(_state(profile="prof"), "claude-sonnet")

        fetch.assert_called_once_with(WS, "prof")
        assert token == "fetched"
        provider = json.loads(paths[0].read_text())["provider"]["databricks-anthropic"]
        assert provider["options"]["apiKey"] == "fetched"

    def test_does_not_fetch_a_token_when_one_is_given(self, agent, tool, _file, paths):
        with patch.object(agent, "get_databricks_token") as fetch:
            _write(agent, _state(), token="given")

        fetch.assert_not_called()

    def test_builds_base_urls_when_state_has_none(self, agent, tool, _file, paths):
        state = _state()
        del state["base_urls"]

        _write(agent, state)

        provider = json.loads(paths[0].read_text())["provider"]["databricks-anthropic"]
        expected = build_opencode_base_urls(WS)["anthropic"]
        assert provider["options"]["baseURL"] == expected

    def test_backs_up_the_existing_file_before_writing(self, agent, tool, _file, paths):
        config, backup = paths
        config.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")

        _write(agent, _state())

        assert json.loads(backup.read_text()) == {"theme": "dark"}
        assert json.loads(config.read_text())["theme"] == "dark"

    def test_keeps_the_mcp_table_when_writing_the_config(self, agent, tool, _file, paths):
        config, _ = paths
        config.write_text(json.dumps({"mcp": {"mine": {"type": "local"}}}), encoding="utf-8")

        _write(agent, _state())

        assert json.loads(config.read_text())["mcp"] == {"mine": {"type": "local"}}

    def test_replaces_a_provider_value_that_is_not_a_table(self, agent, tool, _file, paths):
        config, _ = paths
        config.write_text(json.dumps({"provider": "oops"}), encoding="utf-8")

        _write(agent, _state())

        written = json.loads(config.read_text())
        assert list(written["provider"]) == ["databricks-anthropic"]


@pytest.mark.parametrize(("agent", "tool", "_file"), AGENTS)
class TestStaleProviderCleanup:
    def test_removes_managed_providers_the_overlay_no_longer_writes(
        self, agent, tool, _file, paths
    ):
        config, _ = paths
        config.write_text(
            json.dumps(
                {
                    "provider": {
                        "databricks-google": {"old": True},
                        "databricks-oss": {"old": True},
                        "other": {"keep": True},
                    }
                }
            ),
            encoding="utf-8",
        )

        _write(agent, _state())

        providers = json.loads(config.read_text())["provider"]
        assert set(providers) == {"databricks-anthropic", "other"}


class TestDatabricksOpenaiRemoval:
    """OpenCode lists databricks-openai among the stale providers; Kilo does not."""

    OLD = {"provider": {"databricks-openai": {"old": True}, "other": {"keep": True}}}

    def test_opencode_removes_it(self, tmp_path, monkeypatch):
        config = tmp_path / "opencode.json"
        monkeypatch.setattr(opencode, "OPENCODE_CONFIG_PATH", config)
        monkeypatch.setattr(opencode, "OPENCODE_BACKUP_PATH", tmp_path / "backup.json")
        monkeypatch.setattr("ucode.config_io.APP_DIR", tmp_path)
        config.write_text(json.dumps(self.OLD), encoding="utf-8")

        _write(opencode, _state())

        providers = json.loads(config.read_text())["provider"]
        assert "databricks-openai" not in providers
        assert providers["other"] == {"keep": True}

    def test_kilo_leaves_it_alone(self, tmp_path, monkeypatch):
        config = tmp_path / "kilo.json"
        monkeypatch.setattr(kilo, "KILO_CONFIG_PATH", config)
        monkeypatch.setattr(kilo, "KILO_BACKUP_PATH", tmp_path / "backup.json")
        monkeypatch.setattr("ucode.config_io.APP_DIR", tmp_path)
        config.write_text(json.dumps(self.OLD), encoding="utf-8")

        _write(kilo, _state())

        providers = json.loads(config.read_text())["provider"]
        assert providers["databricks-openai"] == {"old": True}
        assert providers["other"] == {"keep": True}


@pytest.mark.parametrize(("agent", "tool", "_file"), AGENTS)
class TestMcpWriters:
    def test_write_creates_the_file_and_the_entry(self, agent, tool, _file, paths):
        config, _ = paths

        removed = agent.write_mcp_server_config("sql", ARGV)

        assert removed is False
        assert json.loads(config.read_text()) == {
            "mcp": {"sql": {"type": "local", "command": ARGV, "enabled": True}}
        }

    def test_write_reports_whether_the_name_was_already_there(self, agent, tool, _file, paths):
        config, _ = paths

        first = agent.write_mcp_server_config("sql", ARGV)
        second = agent.write_mcp_server_config("sql", [*ARGV, "--again"])
        other = agent.write_mcp_server_config("other", ARGV)

        assert (first, second, other) == (False, True, False)
        mcp = json.loads(config.read_text())["mcp"]
        assert mcp["sql"]["command"] == [*ARGV, "--again"]
        assert mcp["other"]["command"] == ARGV

    def test_write_backs_up_the_existing_file(self, agent, tool, _file, paths):
        config, backup = paths
        config.write_text(json.dumps({"model": "m"}), encoding="utf-8")

        agent.write_mcp_server_config("sql", ARGV)

        assert json.loads(backup.read_text()) == {"model": "m"}

    def test_write_replaces_an_mcp_value_that_is_not_a_table(self, agent, tool, _file, paths):
        config, _ = paths
        config.write_text(json.dumps({"mcp": ["x"]}), encoding="utf-8")

        removed = agent.write_mcp_server_config("sql", ARGV)

        assert removed is False
        assert list(json.loads(config.read_text())["mcp"]) == ["sql"]

    def test_entry_copies_the_argv(self, agent, tool, _file, paths):
        argv = list(ARGV)

        entry = agent.build_mcp_server_entry(argv)
        argv.append("--extra")

        assert entry["command"] == ARGV

    def test_remove_without_the_file_creates_nothing(self, agent, tool, _file, paths):
        config, backup = paths

        assert agent.remove_mcp_server_config("sql") is False

        assert not config.exists()
        assert not backup.exists()

    def test_remove_of_a_missing_name_leaves_the_file_as_it_was(self, agent, tool, _file, paths):
        config, _ = paths
        text = '{"mcp": {"keep": {}},   "model": "m"}'
        config.write_text(text, encoding="utf-8")

        assert agent.remove_mcp_server_config("sql") is False

        assert config.read_text() == text

    def test_remove_ignores_an_mcp_value_that_is_not_a_table(self, agent, tool, _file, paths):
        config, _ = paths
        config.write_text(json.dumps({"mcp": ["x"]}), encoding="utf-8")

        assert agent.remove_mcp_server_config("sql") is False

        assert json.loads(config.read_text()) == {"mcp": ["x"]}

    def test_batch_reports_only_names_that_were_there(self, agent, tool, _file, paths):
        config, backup = paths
        config.write_text(json.dumps({"mcp": {"gone": {}, "keep": {}}}), encoding="utf-8")

        removed = agent.write_user_mcp_servers({"new": {"type": "local"}}, {"gone", "absent"})

        assert removed == {"gone"}
        assert json.loads(config.read_text())["mcp"] == {"keep": {}, "new": {"type": "local"}}
        assert json.loads(backup.read_text())["mcp"] == {"gone": {}, "keep": {}}
