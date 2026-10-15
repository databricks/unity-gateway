"""Offline process/lifetime checks for opt-in Codex runtime launches."""

from __future__ import annotations

import json
import socket
from urllib.parse import urlsplit

import pytest

from ucode.agents import codex
from ucode.smart_routing import orchestrator, session_env, v2


def relay_address(argv):
    import tomlkit

    provider = next(arg for arg in argv if arg.startswith("model_providers.Databricks="))
    config = tomlkit.parse("provider=" + provider.split("=", 1)[1])["provider"]
    url = urlsplit(config["base_url"])
    return url.hostname, url.port


def enable_runtime(monkeypatch):
    monkeypatch.setenv("ENABLE_SMART_ROUTING_SUBAGENT_ONLY", "1")
    monkeypatch.setenv("ENABLE_SMART_ROUTER_ORCHESTRATOR", "1")
    monkeypatch.setenv("ENABLE_CODEX_ORCHESTRATOR_V10", "1")
    monkeypatch.setattr(codex, "agent_version", lambda binary: "0.160.0")
    monkeypatch.setattr(codex, "ug_version", lambda: "test")
    monkeypatch.setattr(codex, "_launch_token", lambda state, workspace, **kwargs: "token")
    monkeypatch.setattr(v2, "_launch_token", lambda state, workspace: "token")


def test_direct_v10_launch_keeps_relay_alive_then_closes_it(monkeypatch):
    enable_runtime(monkeypatch)
    observed = []

    class Child:
        def __init__(self, argv):
            observed.append(relay_address(argv))
            assert argv[-1] == "inspect"
            assert "app-server" not in argv

        def wait(self):
            with socket.create_connection(observed[0], timeout=2):
                pass
            return 7

    monkeypatch.setattr(v2.subprocess_cross_os, "popen", Child)
    monkeypatch.setattr(v2, "exec_or_spawn", lambda argv: pytest.fail("relay owner cannot exec"))
    with pytest.raises(SystemExit) as result:
        v2.launch_codex(
            {"workspace": "https://workspace.example", "codex_models": ["test-model"]},
            ["inspect"],
            binary="codex",
            start_model="test-model",
            render_overlay=codex.render_overlay,
        )
    assert result.value.code == 7
    with socket.socket() as connection:
        assert connection.connect_ex(observed[0]) != 0


@pytest.mark.parametrize("version", ["0.145.0", "unknown"])
def test_v10_version_guard_does_not_start_a_process(version, monkeypatch):
    enable_runtime(monkeypatch)
    monkeypatch.setattr(codex, "agent_version", lambda binary: version)
    session_env.start_session()
    overlay = codex.render_overlay("https://workspace.example", "test-model")
    original = json.loads(json.dumps(overlay))
    with pytest.raises(RuntimeError, match="0.160.0"):
        with v2._codex_runtime_proxy({"workspace": "https://workspace.example"}, overlay, "codex"):
            pytest.fail("unsupported runtime became active")
    assert overlay == original


def test_runtime_checks_current_session_controls(monkeypatch):
    enable_runtime(monkeypatch)
    session_env.start_session()
    assert orchestrator.codex_runtime_enabled()
    session_env.set_session_environment({"ENABLE_CODEX_ORCHESTRATOR_V10": "0"})
    assert not orchestrator.codex_runtime_enabled()
    session_env.set_session_environment({})
    assert orchestrator.codex_runtime_enabled()
    session_env.set_session_environment(
        {"ENABLE_SMART_ROUTING_SUBAGENT_ONLY": "0", "ENABLE_SMART_ROUTING_V2": "0"}
    )
    assert not orchestrator.codex_runtime_enabled()


@pytest.mark.parametrize("ready", [False, True])
def test_first_prompt_path_cleans_up_relay_and_owned_processes(ready, monkeypatch):
    enable_runtime(monkeypatch)
    monkeypatch.setenv("ENABLE_SMART_ROUTING_SUBAGENT_ONLY", "0")
    monkeypatch.setenv("ENABLE_SMART_ROUTING_V2", "1")
    monkeypatch.setattr(v2, "_wait_for_app_server", lambda port, timeout: ready)
    from ucode.smart_routing import codex_interposer

    stopped = []
    monkeypatch.setattr(
        codex_interposer,
        "start_interposer_thread",
        lambda *args, **kwargs: (7031, lambda: stopped.append(True)),
    )
    processes = []
    address = []

    class Child:
        def __init__(self, argv, **kwargs):
            self.terminated = False
            self.app = argv[1] == "app-server"
            if self.app:
                address.append(relay_address(argv))
            processes.append(self)
            with socket.create_connection(address[0], timeout=2):
                pass

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            self.terminated = True

    monkeypatch.setattr(v2.subprocess_cross_os, "popen", Child)
    with pytest.raises(SystemExit if ready else RuntimeError):
        v2.launch_codex(
            {"workspace": "https://workspace.example", "codex_models": ["test-model"]},
            [],
            binary="codex",
            start_model="test-model",
            render_overlay=codex.render_overlay,
        )
    assert len(processes) == (2 if ready else 1)
    assert processes[0].terminated
    assert stopped == ([True] if ready else [])
    with socket.socket() as connection:
        assert connection.connect_ex(address[0]) != 0


@pytest.mark.parametrize("custom_enabled", [False, True])
def test_proxy_auth_uses_the_same_custom_oauth_gate_as_the_launch(custom_enabled, monkeypatch):
    token_fn = codex._launch_token
    enable_runtime(monkeypatch)
    monkeypatch.setattr(codex, "_launch_token", token_fn)
    monkeypatch.setenv("ENABLE_CUSTOM_OAUTH_FROM_CLI", "1" if custom_enabled else "0")
    calls = []
    monkeypatch.setattr(
        codex,
        "get_custom_client_token",
        lambda *args, **kwargs: calls.append(("custom", kwargs["force_refresh"])) or "custom-token",
    )
    monkeypatch.setattr(
        codex,
        "get_databricks_token",
        lambda *args, **kwargs: (
            calls.append(("profile", kwargs["force_refresh"])) or "profile-token"
        ),
    )
    session_env.start_session()
    state = {
        "workspace": "https://workspace.example",
        "custom_oauth": {
            "client_id": "client",
            "redirect_url": "http://localhost",
            "scopes": ["scope"],
        },
    }
    overlay = codex.render_overlay(state["workspace"], "test-model")
    with v2._codex_runtime_proxy(state, overlay, "codex"):
        assert calls == [("custom" if custom_enabled else "profile", True)]
