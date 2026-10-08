"""Component coverage for launcher provider selection; no live model/auth requests."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ucode.agents import LaunchOptions, claude
from ucode.config_io import write_json_file
from ucode.mcp_web_search import AUTOMATIC_PROVIDER, MANAGED_ENTRY_FLAG, PROVIDER_ENV
from ucode.state import load_state, save_state


@pytest.fixture
def search_config(tmp_path, monkeypatch):
    config_dir = tmp_path / "claude"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv(PROVIDER_ENV, "external")
    monkeypatch.setattr(claude, "CLAUDE_SETTINGS_PATH", config_dir / "ucode-settings.json")
    monkeypatch.setattr(claude, "CLAUDE_USER_SETTINGS_PATH", config_dir / "settings.json")
    monkeypatch.setattr(claude, "CLAUDE_BACKUP_PATH", tmp_path / "backup.json")
    managed_path = tmp_path / "managed" / "managed-settings.json"
    monkeypatch.setattr(claude, "_managed_settings_path", lambda: managed_path)
    entry = {
        "type": "stdio",
        "command": "/legacy/install/ug",
        "args": ["mcp", "web-search"],
        "env": {"DATABRICKS_HOST": "https://example.databricks.com", "UCODE_WEB_SEARCH_MODEL": "m"},
    }
    config = {"mcpServers": {"web_search": entry, "my_search": copy.deepcopy(entry)}}
    write_json_file(claude.claude_mcp_config_path(), config)
    state = {
        "workspace": "https://example.databricks.com",
        "codex_models": ["m"],
        claude.WEB_SEARCH_MCP_STATE_KEY: copy.deepcopy(entry),
    }
    return SimpleNamespace(config=config, state=state, project=project, managed=managed_path)


def _override(args):
    return json.loads(args[args.index("--mcp-config") + 1])["mcpServers"]["web_search"]


def _exchange(entry, *, provider="external", call=False):
    requests = [{"id": 1, "method": "initialize"}, {"id": 2, "method": "tools/list"}]
    if call:
        requests.append(
            {
                "id": 3,
                "method": "tools/call",
                "params": {"name": "web_search", "arguments": {"query": "q"}},
            }
        )
    result = subprocess.run(
        [entry["command"], *entry["args"]],
        input="".join(json.dumps({"jsonrpc": "2.0", **req}) + "\n" for req in requests),
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            PROVIDER_ENV: provider,
            **entry.get("env", {}),
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    return [json.loads(line) for line in result.stdout.splitlines()]


def test_external_launch_uses_current_helper_and_preserves_configuration(search_config):
    before = claude.claude_mcp_config_path().read_bytes()
    before_state = copy.deepcopy(search_config.state)
    args = claude._external_web_search_args(search_config.state, ["-p", "hello"])
    entry = _override(args)
    assert entry["command"] == sys.executable
    responses = _exchange(entry, call=True)
    assert "instructions" not in responses[0]["result"]
    assert responses[1]["result"]["tools"] == []
    assert responses[2]["result"]["isError"] is True
    assert "external search provider" in responses[2]["result"]["content"][0]["text"]
    assert claude.claude_mcp_config_path().read_bytes() == before
    assert search_config.state == before_state


def test_concurrent_standalone_and_custom_helpers_remain_available(search_config):
    external = _override(claude._external_web_search_args(search_config.state, []))
    standalone = {
        **external,
        "args": ["-m", "ucode.cli", "mcp", "web-search", MANAGED_ENTRY_FLAG],
        "env": {},
    }
    custom = {**standalone, "args": ["-m", "ucode.cli", "mcp", "web-search"]}
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [
            executor.submit(_exchange, external),
            executor.submit(_exchange, standalone, provider="ucode"),
            executor.submit(_exchange, custom),
        ]
        responses = [future.result() for future in futures]
    assert responses[0][1]["result"]["tools"] == []
    for response in responses[1:]:
        assert response[1]["result"]["tools"][0]["name"] == "web_search"


def test_capabilities_are_machine_readable_without_auth(search_config):
    result = subprocess.run(
        [sys.executable, "-m", "ucode.cli", "mcp", "web-search", "--capabilities"],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        input="",
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    assert json.loads(result.stdout) == {
        "external_provider_contract": 1,
        "provider_env": PROVIDER_ENV,
        "managed_entry_flag": MANAGED_ENTRY_FLAG,
        "automatic_provider": AUTOMATIC_PROVIDER,
    }


@pytest.mark.parametrize("mode", ["fresh", "disabled", "strict", "standalone"])
def test_no_override_when_generated_server_is_absent_or_excluded(search_config, monkeypatch, mode):
    args = ["-p", "hello"]
    if mode == "fresh":
        del search_config.config["mcpServers"]["web_search"]
    elif mode == "disabled":
        search_config.config["projects"] = {
            str(search_config.project): {"disabledMcpServers": ["web_search"]}
        }
    elif mode == "strict":
        args.append("--strict-mcp-config")
    else:
        monkeypatch.delenv(PROVIDER_ENV)
    write_json_file(claude.claude_mcp_config_path(), search_config.config)
    before = claude.claude_mcp_config_path().read_bytes()
    assert claude._external_web_search_args(search_config.state, args) == args
    assert claude.claude_mcp_config_path().read_bytes() == before


@pytest.mark.parametrize(
    "mode", ["edited", "no_state", "unknown_command", "extra_env", "bad_config", "bad_servers"]
)
def test_unknown_ownership_fails_without_changes(search_config, mode):
    if mode == "edited":
        search_config.config["mcpServers"]["web_search"]["env"]["UCODE_WEB_SEARCH_MODEL"] = "edited"
    elif mode == "no_state":
        del search_config.state[claude.WEB_SEARCH_MCP_STATE_KEY]
    elif mode == "unknown_command":
        search_config.config["mcpServers"]["web_search"]["command"] = "/custom/tool"
        search_config.state[claude.WEB_SEARCH_MCP_STATE_KEY] = search_config.config["mcpServers"][
            "web_search"
        ]
    elif mode == "extra_env":
        search_config.config["mcpServers"]["web_search"]["env"]["CUSTOM"] = "1"
        search_config.state[claude.WEB_SEARCH_MCP_STATE_KEY] = search_config.config["mcpServers"][
            "web_search"
        ]
    elif mode == "bad_servers":
        search_config.config["mcpServers"] = []
    write_json_file(claude.claude_mcp_config_path(), search_config.config)
    if mode == "bad_config":
        claude.claude_mcp_config_path().write_text("not JSON")
    before = claude.claude_mcp_config_path().read_bytes()
    with pytest.raises(RuntimeError, match="Cannot safely select external web search"):
        claude._external_web_search_args(search_config.state, [])
    assert claude.claude_mcp_config_path().read_bytes() == before


@pytest.mark.parametrize(
    "scope", ["project", "ancestor", "local", "managed", "exclusive", "policy", "env", "caller"]
)
def test_conflicting_scope_is_preserved(search_config, scope):
    conflicting = {"mcpServers": {"web_search": {"command": "custom"}}}
    args = []
    if scope == "project":
        write_json_file(search_config.project / ".mcp.json", conflicting)
    elif scope == "ancestor":
        write_json_file(search_config.project.parent / ".mcp.json", conflicting)
    elif scope == "local":
        search_config.config["projects"] = {str(search_config.project): conflicting}
        write_json_file(claude.claude_mcp_config_path(), search_config.config)
    elif scope == "managed":
        write_json_file(search_config.managed, {"managedMcpServers": conflicting["mcpServers"]})
    elif scope == "exclusive":
        write_json_file(search_config.managed.with_name("managed-mcp.json"), conflicting)
    elif scope == "policy":
        write_json_file(search_config.managed, {"deniedMcpServers": [{"serverName": "web_search"}]})
    elif scope == "env":
        write_json_file(claude.CLAUDE_USER_SETTINGS_PATH, {"env": {PROVIDER_ENV: "ucode"}})
    else:
        args = ["--mcp-config", json.dumps(conflicting)]
    before = {path: path.read_bytes() for path in search_config.project.parent.rglob("*.json")}
    with pytest.raises(RuntimeError, match="Cannot safely select external web search"):
        claude._external_web_search_args(search_config.state, args)
    assert all(path.read_bytes() == data for path, data in before.items())


@pytest.mark.parametrize("style", ["separate", "equals", "separator"])
def test_caller_mcp_config_and_prompt_arguments_survive(search_config, style):
    caller = json.dumps({"mcpServers": {"web-search": {"command": "isaac-search"}}})
    if style == "separate":
        args = ["--mcp-config", caller, "-p", "hello"]
    elif style == "equals":
        args = [f"--mcp-config={caller}", "-p", "hello"]
    else:
        args = ["-p", "--", "hello"]
    result = claude._external_web_search_args(search_config.state, args)
    assert _override(result)["args"][-1] == MANAGED_ENTRY_FLAG
    if style == "separator":
        assert result[-2:] == ["--", "hello"]
    else:
        assert caller in result
        assert result[-2:] == ["-p", "hello"]


@pytest.mark.parametrize("routing", ["off", "on", "fallback", "relayed"])
def test_launch_paths_receive_external_override(search_config, monkeypatch, routing):
    calls = []
    # External boundaries are captured; config reading and argument composition stay real.
    monkeypatch.setattr(claude, "get_databricks_token", lambda *a, **kw: "fixture")
    monkeypatch.setattr(claude, "_resolve_launch_binary", lambda binary: binary)
    monkeypatch.setattr(claude, "exec_or_spawn", lambda argv: calls.append(argv))
    monkeypatch.setattr(claude, "_launch_relayed", lambda state, binary, args: calls.append(args))
    monkeypatch.setattr(
        claude, "claude_recipe_session", lambda settings, _path: nullcontext(settings)
    )

    def spawn(argv):
        calls.append(argv)
        return SimpleNamespace(wait=lambda: 0)

    monkeypatch.setattr(claude.subprocess_cross_os, "popen", spawn)

    def routed(state, args, **kwargs):
        if routing == "fallback":
            raise claude.smart_routing_v2.ClaudeRoutingSetupError("fixture setup failure")
        calls.append(args)

    monkeypatch.setattr(claude.smart_routing_v2, "launch_claude", routed)
    search_config.state["claude_relayed"] = routing == "relayed"
    with patch.dict(os.environ):
        if routing == "fallback":
            with pytest.raises(SystemExit) as exc:
                claude.launch(
                    search_config.state,
                    ["-p", "hello"],
                    options=LaunchOptions(launch_smart_routing=True),
                )
            assert exc.value.code == 0
        else:
            claude.launch(
                search_config.state,
                ["-p", "hello"],
                options=LaunchOptions(launch_smart_routing=routing == "on"),
            )
    assert len(calls) == 1
    assert _override(calls[0])["env"][PROVIDER_ENV] == "external"


@pytest.mark.parametrize("has_model", [True, False])
def test_external_configuration_preserves_saved_entry_and_ownership(
    search_config, monkeypatch, has_model
):
    monkeypatch.setattr(
        claude, "refresh_managed_config", lambda state: SimpleNamespace(manifest=None)
    )
    monkeypatch.setattr(claude, "_reconcile_managed_settings", lambda *a: None)
    monkeypatch.setattr(
        claude,
        "_register_web_search_mcp",
        lambda *a, **kw: pytest.fail("external mode registered search"),
    )
    if not has_model:
        search_config.state.pop("codex_models")
    before = claude.claude_mcp_config_path().read_bytes()
    state = claude.write_tool_config(search_config.state, "claude-model")
    assert (
        state[claude.WEB_SEARCH_MCP_STATE_KEY]
        == search_config.state[claude.WEB_SEARCH_MCP_STATE_KEY]
    )
    assert claude.claude_mcp_config_path().read_bytes() == before
    assert PROVIDER_ENV not in claude.CLAUDE_SETTINGS_PATH.read_text()


def test_standalone_refresh_preserves_custom_user_entry(search_config, monkeypatch):
    monkeypatch.delenv(PROVIDER_ENV)
    search_config.config["mcpServers"]["web_search"]["command"] = "custom"
    write_json_file(claude.claude_mcp_config_path(), search_config.config)
    monkeypatch.setattr(
        claude, "remove_claude_mcp_server", lambda *a: pytest.fail("removed custom")
    )
    monkeypatch.setattr(claude, "add_claude_mcp_server", lambda *a: pytest.fail("replaced custom"))
    before = claude.claude_mcp_config_path().read_bytes()
    assert not claude._register_web_search_mcp("https://example.databricks.com", "m")
    assert claude.claude_mcp_config_path().read_bytes() == before


def test_standalone_without_search_model_preserves_ownership_for_external_launch(
    search_config, monkeypatch, capsys
):
    """Scenario: a standalone refresh finds no search model before an Isaac-style launch.

    Expected: the installed entry and ownership survive, allowing a verified launch override.
    """
    monkeypatch.setenv(PROVIDER_ENV, "ucode")
    # The workspace service and OS-managed writer are external to this component journey.
    monkeypatch.setattr(
        claude, "refresh_managed_config", lambda state: SimpleNamespace(manifest=None)
    )
    monkeypatch.setattr(claude, "_reconcile_managed_settings", lambda *a: None)
    monkeypatch.setattr(
        claude, "remove_claude_mcp_server", lambda *a: pytest.fail("removed installed search")
    )
    monkeypatch.setattr(
        claude, "add_claude_mcp_server", lambda *a: pytest.fail("replaced installed search")
    )
    before = claude.claude_mcp_config_path().read_bytes()
    ownership = copy.deepcopy(search_config.state[claude.WEB_SEARCH_MCP_STATE_KEY])
    save_state({**search_config.state, "codex_models": []})

    claude.write_tool_config(load_state(), "claude-model")
    state = load_state()
    assert claude.claude_mcp_config_path().read_bytes() == before
    assert state.get(claude.WEB_SEARCH_MCP_STATE_KEY) == ownership

    monkeypatch.setenv(PROVIDER_ENV, AUTOMATIC_PROVIDER)
    args = claude._external_web_search_args(state, ["-p", "hello"])
    assert _override(args)["env"][PROVIDER_ENV] == "external"
    assert args[:2] == ["-p", "hello"]
    assert claude.claude_mcp_config_path().read_bytes() == before
    assert load_state()[claude.WEB_SEARCH_MCP_STATE_KEY] == ownership
    output = capsys.readouterr().out
    assert "Cannot safely select external web search" not in output
    assert "Preserving web_search" not in output


def test_standalone_refresh_recovers_after_search_models_return(search_config, monkeypatch, capsys):
    """Scenario: a known generated registration outlives temporary missing search models.

    Expected: a later standalone refresh updates it and its ownership without a warning.
    """
    monkeypatch.setenv(PROVIDER_ENV, "ucode")
    monkeypatch.setattr(
        claude, "refresh_managed_config", lambda state: SimpleNamespace(manifest=None)
    )
    monkeypatch.setattr(claude, "_reconcile_managed_settings", lambda *a: None)
    calls = []

    # Model only Claude's external CLI writes; ownership checks and persistence stay real.
    def remove_server(name, scope):
        calls.append(("remove", name, scope))
        config = json.loads(claude.claude_mcp_config_path().read_text())
        del config["mcpServers"][name]
        write_json_file(claude.claude_mcp_config_path(), config)

    def add_server(name, entry):
        calls.append(("add", name))
        config = json.loads(claude.claude_mcp_config_path().read_text())
        config["mcpServers"][name] = entry
        write_json_file(claude.claude_mcp_config_path(), config)

    monkeypatch.setattr(claude, "remove_claude_mcp_server", remove_server)
    monkeypatch.setattr(claude, "add_claude_mcp_server", add_server)
    before = claude.claude_mcp_config_path().read_bytes()
    save_state({**search_config.state, "codex_models": []})

    claude.write_tool_config(load_state(), "claude-model")
    assert claude.claude_mcp_config_path().read_bytes() == before
    assert calls == []

    state = load_state()
    state["codex_models"] = ["restored-search-model"]
    claude.write_tool_config(state, "claude-model")
    assert calls == [("remove", "web_search", "user"), ("add", "web_search")]
    config = json.loads(claude.claude_mcp_config_path().read_text())
    installed = config["mcpServers"]["web_search"]
    assert installed["env"]["UCODE_WEB_SEARCH_MODEL"] == "restored-search-model"
    assert installed["args"] == ["mcp", "web-search", MANAGED_ENTRY_FLAG]
    assert load_state()[claude.WEB_SEARCH_MCP_STATE_KEY] == installed
    assert config["mcpServers"]["my_search"] == search_config.config["mcpServers"]["my_search"]
    assert "Preserving web_search" not in capsys.readouterr().out


def test_stale_shared_state_preserves_new_registration_and_falls_back(
    search_config, monkeypatch, capsys
):
    # Reproduce the existing whole-workspace save race deterministically: an older
    # launch saves its snapshot after standalone setup has upgraded the registration.
    save_state(search_config.state)
    stale = load_state()
    new_entry = {
        **search_config.config["mcpServers"]["web_search"],
        "args": ["mcp", "web-search", MANAGED_ENTRY_FLAG],
    }
    search_config.config["mcpServers"]["web_search"] = new_entry
    write_json_file(claude.claude_mcp_config_path(), search_config.config)
    save_state({**search_config.state, claude.WEB_SEARCH_MCP_STATE_KEY: new_entry})
    save_state(stale)
    state = load_state()
    assert state[claude.WEB_SEARCH_MCP_STATE_KEY] != new_entry
    before = claude.claude_mcp_config_path().read_bytes()

    monkeypatch.setenv(PROVIDER_ENV, AUTOMATIC_PROVIDER)
    assert claude._external_web_search_args(state, ["-p", "hello"]) == ["-p", "hello"]
    assert os.environ[PROVIDER_ENV] == "ucode"
    assert "duplicates may remain" in capsys.readouterr().out
    monkeypatch.setattr(
        claude, "remove_claude_mcp_server", lambda *a: pytest.fail("removed newer entry")
    )
    monkeypatch.setattr(
        claude, "add_claude_mcp_server", lambda *a: pytest.fail("replaced newer entry")
    )
    assert not claude._register_web_search_mcp(
        state["workspace"], "m", previous_entry=state[claude.WEB_SEARCH_MCP_STATE_KEY]
    )
    assert claude.claude_mcp_config_path().read_bytes() == before


def test_automatic_selection_uses_verified_override(search_config, monkeypatch):
    monkeypatch.setenv(PROVIDER_ENV, AUTOMATIC_PROVIDER)
    entry = _override(claude._external_web_search_args(search_config.state, []))
    assert _exchange(entry)[1]["result"]["tools"] == []


@pytest.mark.parametrize("provider", ["external", AUTOMATIC_PROVIDER])
@pytest.mark.parametrize("scope", ["alias", "strict", "project"])
def test_only_verified_override_suppresses_marked_helpers(
    search_config, monkeypatch, provider, scope
):
    monkeypatch.setenv(PROVIDER_ENV, provider)
    # Users may copy a generated helper into their own registration. Its marker
    # cannot transfer ownership of that new registration to the launcher.
    caller_entry = {
        "type": "stdio",
        "command": sys.executable,
        "args": ["-m", "ucode.cli", "mcp", "web-search", MANAGED_ENTRY_FLAG],
        "env": copy.deepcopy(search_config.config["mcpServers"]["web_search"]["env"]),
    }
    args = []
    if scope == "alias":
        search_config.config["mcpServers"]["my_search"] = caller_entry
    elif scope == "strict":
        args = [
            "--strict-mcp-config",
            "--mcp-config",
            json.dumps({"mcpServers": {"web_search": caller_entry}}),
        ]
    else:
        del search_config.config["mcpServers"]["web_search"]
        write_json_file(
            search_config.project / ".mcp.json", {"mcpServers": {"web_search": caller_entry}}
        )
    write_json_file(claude.claude_mcp_config_path(), search_config.config)
    before = claude.claude_mcp_config_path().read_bytes()

    launch_args = claude._external_web_search_args(search_config.state, args)
    if scope == "alias":
        assert _exchange(_override(launch_args), provider=provider)[1]["result"]["tools"] == []
    else:
        assert launch_args == args
    response = _exchange(caller_entry, provider=provider)
    assert response[1]["result"]["tools"][0]["name"] == "web_search"
    assert claude.claude_mcp_config_path().read_bytes() == before


def test_repeated_caller_mcp_options_keep_override_in_last_group(search_config):
    first = json.dumps({"mcpServers": {"one": {"command": "custom"}}})
    last = json.dumps({"mcpServers": {"two": {"command": "custom"}}})
    args = ["--mcp-config", first, "--mcp-config", last, "-p", "hello"]
    result = claude._external_web_search_args(search_config.state, args)
    assert result[:3] == args[:3]
    assert json.loads(result[3])["mcpServers"]["web_search"]["env"][PROVIDER_ENV] == "external"
    assert result[4:] == args[3:]


def test_invalid_selection_fails_before_configuration(search_config, monkeypatch):
    monkeypatch.setenv(PROVIDER_ENV, "unknown")
    before = claude.claude_mcp_config_path().read_bytes()
    with pytest.raises(RuntimeError, match="must be"):
        claude.write_tool_config(search_config.state, "claude-model")
    assert claude.claude_mcp_config_path().read_bytes() == before
