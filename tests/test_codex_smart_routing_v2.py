from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from ucode import agents, codex_config, custom_oauth, managed_files
from ucode.agents import LaunchOptions, codex, codex_catalog
from ucode.constants import ISAAC_LAUNCH_MODE_ENV_VAR, ISAAC_OMNI_LAUNCH_MODE
from ucode.smart_routing import codex_interposer, codex_routing, v2

WS = "https://example.databricks.com"


@pytest.fixture(autouse=True)
def native_catalog(monkeypatch, tmp_path):
    prepare = codex_catalog.prepare_codex_catalog
    monkeypatch.setattr(v2, "codex_managed_config_path", lambda: tmp_path / "managed.toml")
    monkeypatch.setattr(
        codex_catalog,
        "prepare_codex_catalog",
        lambda binary, names, *, workspace, get_token, include_bundled: {
            "models": [{"slug": name, "multi_agent_version": "v2"} for name in names]
        },
    )
    return prepare


def test_plaintext_catalog_preserves_source_menu_and_other_metadata(tmp_path):
    source = tmp_path / "user.json"
    original = {
        "models": [
            {
                "slug": "gpt-6-luna",
                "multi_agent_version": "v2",
                "multi_agent_tools_in_code_mode": False,
                "future": {"x": 1},
            },
            {"slug": "glm-5-3-flash", "multi_agent_version": None},
            {"slug": "hidden-alias", "multi_agent_version": "v2", "visibility": "hide"},
        ]
    }
    source.write_text(json.dumps(original))
    before = source.read_bytes()
    output = tmp_path / "session.json"
    assert v2._codex_routing_catalog(
        "codex", str(source), [], output, workspace=WS, get_token=lambda: "token"
    ) == str(output)
    expected = json.loads(before)
    for model in expected["models"]:
        model["multi_agent_tools_in_code_mode"] = True
    assert json.loads(output.read_text()) == expected
    assert source.read_bytes() == before


def test_plaintext_catalog_uses_native_builder_without_existing_catalog(tmp_path, monkeypatch):
    calls = []
    token_calls = []

    def get_token():
        token_calls.append(True)
        return "launch-token"

    def prepare(binary, names, *, workspace, get_token, include_bundled):
        calls.append((binary, names, workspace, get_token, get_token(), include_bundled))
        return {"models": [{"slug": names[0], "multi_agent_version": "v2"}]}

    monkeypatch.setattr(codex_catalog, "prepare_codex_catalog", prepare)
    output = tmp_path / "session.json"
    v2._codex_routing_catalog(
        "/native/codex", None, ["model-a"], output, workspace=WS, get_token=get_token
    )
    assert calls == [("/native/codex", ["model-a"], WS, get_token, "launch-token", True)]
    assert token_calls == [True]
    assert json.loads(output.read_text())["models"][0] == {
        "slug": "model-a",
        "multi_agent_version": "v2",
        "multi_agent_tools_in_code_mode": True,
    }


def test_plaintext_catalog_rejects_unreadable_source(tmp_path):
    with pytest.raises(RuntimeError, match="Cannot load the Codex model catalog"):
        v2._codex_routing_catalog(
            "codex",
            str(tmp_path / "missing"),
            [],
            tmp_path / "out",
            workspace=WS,
            get_token=lambda: "token",
        )


@pytest.mark.parametrize("row", [None, "not-a-model"])
def test_plaintext_catalog_rejects_non_object_rows(tmp_path, row):
    source = tmp_path / "invalid.json"
    source.write_text(json.dumps({"models": [row]}), encoding="utf-8")

    with pytest.raises(RuntimeError, match="Cannot load the Codex model catalog"):
        v2._codex_routing_catalog(
            "codex", source, [], tmp_path / "out", workspace=WS, get_token=lambda: "token"
        )


@pytest.mark.parametrize(
    "mode,published", [(None, False), (True, False), (False, False), (False, True)]
)
def test_plaintext_catalog_respects_managed_precedence(tmp_path, monkeypatch, mode, published):
    monkeypatch.setattr(
        managed_files,
        "published_agent_config_marker",
        lambda: {"workspace": WS} if published else None,
    )
    source = tmp_path / "admin.json"
    source.write_text(
        json.dumps({"models": [{"slug": "admin-model", "multi_agent_tools_in_code_mode": mode}]})
    )
    before = source.read_bytes()
    managed = tmp_path / "managed.toml"
    managed.write_text('model_catalog_json = "admin.json"\n')
    output = tmp_path / "session.json"
    if mode is False:
        command = "sudo ug configure --global" if published else "ug configure --agents codex"
        with pytest.raises(RuntimeError, match=command) as error:
            v2._codex_routing_catalog(
                "codex",
                tmp_path / "ignored-user.json",
                [],
                output,
                workspace=WS,
                get_token=lambda: "token",
            )
        if published:
            assert "administrator" in str(error.value)
            assert "ug configure --agents codex" not in str(error.value)
        assert "use a Codex version with code-mode support" in str(error.value)
        assert "configure preserves native capabilities" in str(error.value)
        assert "skip routing encrypted assignments" in str(error.value)
    else:
        assert v2._codex_routing_catalog(
            "codex",
            tmp_path / "ignored-user.json",
            [],
            output,
            workspace=WS,
            get_token=lambda: "token",
        ) == str(source)
    assert source.read_bytes() == before
    assert not output.exists()


def test_smart_routing_switch_message_is_boxed():
    message = v2.format_routing_notice("model-x", "Because X.")

    assert message == (
        "┌───────────────────────────────────────────────────────────────────────────┐\n"
        "│ Using Unity Gateway Smart Router.                                         │\n"
        "│ Selected Model : model-x                                                  │\n"
        "│ Reason : Because X.                                                       │\n"
        "│ Spawned subagents are routed independently based on their own complexity. │\n"
        "└───────────────────────────────────────────────────────────────────────────┘"
    )


def test_smart_routing_switch_message_wraps_to_fixed_width():
    message = v2.format_routing_notice(
        "model-x",
        "This rationale is deliberately long enough to wrap onto another line "
        "without making the routing box wider.",
    )

    lines = message.splitlines()
    assert len({len(line) for line in lines}) == 1
    assert lines[0] == "┌" + ("─" * 75) + "┐"
    assert "│ Reason : This rationale is deliberately long enough to wrap onto another  │" in lines
    assert "│ line without making the routing box wider.                                │" in lines


class TestLaunchCodex:
    @pytest.mark.parametrize(
        ("installed_version", "accept_upgrade", "upgraded_version", "auto_upgrade"),
        [
            ("0.171.0", False, None, False),
            ("0.170.0", True, "0.171.0", False),
            ("0.170.0", True, "0.171.0", True),
            ("0.170.0", False, None, False),
            ("0.170.0", True, None, False),
            ("0.170.0", True, "0.170.0", False),
            ("unknown", True, "unknown", False),
            ("unknown", True, "unknown", True),
            ("0.170.0", True, "unknown", False),
        ],
        ids=[
            "supported",
            "upgraded",
            "auto-upgraded",
            "declined",
            "failed",
            "still-old",
            "unknown",
            "unknown-auto-upgrade",
            "unknown-after-upgrade",
        ],
    )
    @pytest.mark.parametrize(
        ("tool_args", "options"),
        [
            ([], LaunchOptions(launch_smart_routing=True)),
            (["fix the parser"], LaunchOptions(launch_smart_routing=True)),
        ],
    )
    def test_codex_smart_routing_launch_dispatches_to_v2(
        self,
        monkeypatch,
        tool_args,
        options,
        installed_version,
        accept_upgrade,
        upgraded_version,
        auto_upgrade,
    ):
        calls = []
        prompts = []
        updates = []
        version = installed_version
        monkeypatch.setattr(codex, "agent_version", lambda *args, **kwargs: version)
        monkeypatch.setenv(agents.AUTO_UPGRADE_AGENT_ENV, "1" if auto_upgrade else "0")
        monkeypatch.setattr(agents.shutil, "which", lambda binary: f"/usr/bin/{binary}")
        monkeypatch.setattr(
            agents,
            "prompt_yes_no_default",
            lambda prompt, *, default: prompts.append((prompt, default)) or accept_upgrade,
        )

        def update_binary(argv, **kwargs):
            nonlocal version
            updates.append(argv)
            if upgraded_version is None:
                raise subprocess.CalledProcessError(1, argv)
            version = upgraded_version
            return subprocess.CompletedProcess(argv, 0)

        # Exercise the shared updater without changing the installed agent binary.
        monkeypatch.setattr(agents.subprocess_cross_os, "run", update_binary)
        monkeypatch.setenv(v2.ENABLE_SMART_ROUTING_ENV_VAR, "1")
        monkeypatch.setattr(codex, "_smart_routing_config_model", lambda state: "gpt-start")
        monkeypatch.setattr(codex, "clear_model_preferences", lambda state: False)

        def launch_v2(state, tool_args, **kwargs):
            assert version == "0.171.0", "unsupported Codex reached execution"
            calls.append((state, tool_args, kwargs))
            raise SystemExit(0)

        monkeypatch.setattr(v2, "launch_codex", launch_v2)
        state = {"workspace": WS}

        allowed = installed_version == "0.171.0" or upgraded_version == "0.171.0"
        with pytest.raises(SystemExit if allowed else RuntimeError) as exc:
            codex.launch(state, tool_args, options=options)

        needs_upgrade = installed_version == "0.170.0"
        assert prompts == (
            [("Upgrade Codex if available?", True)] if needs_upgrade and not auto_upgrade else []
        )
        assert updates == ([["codex", "update"]] if needs_upgrade and accept_upgrade else [])
        if not allowed:
            assert "requires Codex 0.171.0" in str(exc.value)
            if version == "unknown":
                assert "codex --version" in str(exc.value)
            assert calls == []
            return
        assert exc.value.code == 0
        assert calls == [
            (
                state,
                tool_args,
                {
                    "binary": "codex",
                    "start_model": "gpt-start",
                    "render_overlay": codex._render_launch_overlay,
                },
            )
        ]

    @pytest.mark.parametrize(
        "tool_args",
        [
            ["exec", "fix this"],
            ["review"],
            ["app-server"],
            ["update"],
            ["--model", "gpt-5.6-sol"],
            ["--model", "gpt-5.6-sol", "--", "fix this"],
            ["fix this"],
        ],
    )
    def test_codex_launch_bypasses_routing_for_other_shapes(self, tmp_path, monkeypatch, tool_args):
        launches = []
        profile_path = tmp_path / "ucode.config.toml"
        profile_path.write_text('model_provider = "ucode-databricks"\n', encoding="utf-8")
        monkeypatch.setenv(v2.ENABLE_SMART_ROUTING_ENV_VAR, "1")
        monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", profile_path)
        monkeypatch.setattr(codex, "clear_model_preferences", lambda state: False)
        monkeypatch.setattr(codex, "agent_version", lambda binary: "0.144.0")
        monkeypatch.setattr(custom_oauth, "get_databricks_token", lambda *_args, **_kw: "token")
        monkeypatch.setattr(v2, "launch_codex", lambda *args, **kwargs: pytest.fail("launched"))
        monkeypatch.setattr(codex, "exec_or_spawn", lambda argv: launches.append(argv))

        codex.launch(
            {"workspace": WS},
            tool_args,
            options=LaunchOptions(),
        )

        assert launches == [["codex", "--config", 'model_provider="ucode-databricks"', *tool_args]]

    @pytest.mark.parametrize(
        ("installed_version", "routed"), [("0.170.0", False), ("0.171.0", True)]
    )
    def test_app_server_routes_only_from_codex_routing_floor(
        self, tmp_path, monkeypatch, capsys, installed_version, routed
    ):
        launches = []
        profile_path = tmp_path / "ucode.config.toml"
        profile_path.write_text('model_provider = "ucode-databricks"\n', encoding="utf-8")
        monkeypatch.setenv(v2.ENABLE_SMART_ROUTING_ENV_VAR, "1")
        monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", profile_path)
        monkeypatch.setattr(codex, "clear_model_preferences", lambda state: False)
        monkeypatch.setattr(codex, "_smart_routing_config_model", lambda state: "gpt-start")
        monkeypatch.setattr(codex, "agent_version", lambda *_args, **_kw: installed_version)
        monkeypatch.setattr(custom_oauth, "get_databricks_token", lambda *_args, **_kw: "token")
        monkeypatch.setattr(
            agents, "prompt_yes_no_default", lambda *_args, **_kw: pytest.fail("prompted")
        )
        monkeypatch.setattr(
            agents.subprocess_cross_os, "run", lambda *_args, **_kw: pytest.fail("updated")
        )
        monkeypatch.setattr(
            v2, "launch_codex", lambda _state, args, **_kw: launches.append(("routed", args))
        )
        monkeypatch.setattr(codex, "exec_or_spawn", lambda argv: launches.append(("plain", argv)))
        tool_args = ["app-server", "--listen", "stdio://"]

        codex.launch({"workspace": WS}, tool_args, options=LaunchOptions(launch_smart_routing=True))

        warning = " ".join(capsys.readouterr().err.split())
        if routed:
            assert launches == [("routed", tool_args)]
            assert "without smart routing" not in warning
        else:
            assert launches == [
                ("plain", ["codex", "--config", 'model_provider="ucode-databricks"', *tool_args])
            ]
            assert (
                "Smart routing requires Codex 0.171.0 or newer; found 0.170.0. "
                "Running the app-server without smart routing."
            ) in warning

    def test_codex_launch_normalizes_cached_bootstrap_model(self, monkeypatch):
        calls = []
        monkeypatch.setattr(codex, "agent_version", lambda _: "0.171.0")
        monkeypatch.setenv(v2.ENABLE_SMART_ROUTING_ENV_VAR, "1")
        monkeypatch.setattr(codex, "custom_catalog_models", lambda: None)
        monkeypatch.setattr(codex, "clear_model_preferences", lambda state: False)
        monkeypatch.setattr(codex, "_smart_routing_config_model", lambda state: None)

        def launch_v2(state, tool_args, **kwargs):
            calls.append(kwargs)
            raise SystemExit(0)

        monkeypatch.setattr(v2, "launch_codex", launch_v2)

        with pytest.raises(SystemExit):
            codex.launch(
                {"workspace": WS, "codex_models": ["system.ai.gpt-5-6-luna"]},
                [],
                options=LaunchOptions(launch_smart_routing=True),
            )

        assert calls[0]["start_model"] == "gpt-5.6-luna"

    @pytest.mark.parametrize("custom_home", [False, True])
    @pytest.mark.parametrize(
        "managed,profile,user,expected",
        [
            ('model = "managed"', 'model = "profile"', 'model = "user"', "managed"),
            ("", 'model = "profile"', 'model = "user"', "profile"),
            ("", "", 'model = "user"', "user"),
            ('model = " "', "model = 12", 'model = "user"', "user"),
            ("", "invalid toml", 'model = "user"', "user"),
            (None, None, None, "gpt-5.6-luna"),
        ],
    )
    def test_startup_config_precedence(
        self, tmp_path, monkeypatch, custom_home, managed, profile, user, expected
    ):
        config_home = tmp_path / "codex"
        config_home.mkdir()
        managed_path = tmp_path / "managed_config.toml"
        profile_path = config_home / "ucode.config.toml"
        user_path = config_home / "config.toml"
        monkeypatch.setenv(v2.ENABLE_SMART_ROUTING_ENV_VAR, "1")
        monkeypatch.delenv("CODEX_HOME", raising=False)
        monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", profile_path)
        monkeypatch.setattr(codex, "codex_managed_config_path", lambda: managed_path)
        monkeypatch.setattr(codex, "agent_version", lambda _: "0.171.0")
        monkeypatch.setattr(codex, "custom_catalog_models", lambda: None)
        if custom_home:
            monkeypatch.setenv("CODEX_HOME", str(config_home))
            monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", tmp_path / "unused.config.toml")
        for path, content in ((managed_path, managed), (profile_path, profile), (user_path, user)):
            if content is not None:
                path.write_text(content)
        calls = []
        monkeypatch.setattr(v2, "launch_codex", lambda *args, **kwargs: calls.append(kwargs))

        # Model resolution and cleanup also run before the actual smart-routing launch.
        codex.default_model({})
        assert codex.clear_model_preferences({}) is False
        codex.launch({"workspace": WS}, [], options=LaunchOptions(launch_smart_routing=True))

        assert calls[0]["start_model"] == expected
        for path, content in ((managed_path, managed), (profile_path, profile), (user_path, user)):
            if content is not None:
                assert path.read_text() == content
        assert codex._smart_routing_config_model({"codex_default_model": "admin"}) == "admin"

    @pytest.mark.parametrize("omnigent", [False, True], ids=["cli", "omnigent"])
    @pytest.mark.parametrize("platform_name", ["posix", "nt"])
    @pytest.mark.parametrize("first_prompt", [False, True])
    @pytest.mark.parametrize(
        "model_args",
        [[], ["--model", "gpt-start"], ["--model=gpt-start"], ["-m", "gpt-start"]],
        ids=["default-model", "split-model", "equals-model", "short-model"],
    )
    def test_owns_app_server_interposer_and_tui_lifecycle(
        self, tmp_path, monkeypatch, platform_name, first_prompt, model_args, omnigent
    ):
        processes = []
        interposer_args = {}
        stopped = []
        token_calls = []
        validation_requests = []
        startup_check_sent = threading.Event()

        def validate(url, token, body, **kwargs):
            validation_requests.append((token, body, kwargs.get("headers")))
            startup_check_sent.set()
            return {"is_supported": True}, None

        monkeypatch.setattr(v2.route_validation, "_http_post_json", validate)
        if omnigent:
            monkeypatch.setenv(ISAAC_LAUNCH_MODE_ENV_VAR, ISAAC_OMNI_LAUNCH_MODE)
        user_config = tmp_path / "config.toml"
        saved_config = (
            b"[features.multi_agent_v2]\nnon_code_mode_only = true\n"
            b'root_agent_usage_hint_text = "user root hint"\n'
            b'subagent_usage_hint_text = "user child hint"\n'
        )
        user_config.write_bytes(saved_config)
        monkeypatch.setenv(v2.ENABLE_SMART_ROUTING_ENV_VAR, "1" if first_prompt else "0")
        monkeypatch.setenv(v2.ENABLE_SUBAGENT_ROUTING_ENV_VAR, "0" if first_prompt else "1")
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        monkeypatch.setattr(v2, "os", SimpleNamespace(name=platform_name, environ=os.environ))
        monkeypatch.setattr(codex, "ug_version", lambda: "0.1.0")
        monkeypatch.setattr(codex, "agent_version", lambda binary: "0.148.0")
        monkeypatch.setattr(v2, "custom_catalog_path", lambda: None)
        monkeypatch.setattr(v2, "custom_catalog_models", lambda: None)

        class FakeProcess:
            def __init__(self, argv, **kwargs):
                self.argv = argv
                self.kwargs = kwargs
                self.terminated = False
                processes.append(self)

            def wait(self, timeout=None):
                return 0 if timeout is not None else 7

            def terminate(self):
                self.terminated = True

            def kill(self):
                raise AssertionError("clean shutdown should not need kill")

            def send_signal(self, _signal):
                raise AssertionError("test does not interrupt the TUI")

        monkeypatch.setattr(v2.subprocess, "Popen", FakeProcess)

        def get_token(workspace, profile, **_kw):
            token_calls.append((workspace, profile))
            return f"token-{len(token_calls)}"

        monkeypatch.setattr(custom_oauth, "get_databricks_token", get_token)
        monkeypatch.setattr(v2, "_free_port", lambda: 41001)
        monkeypatch.setattr(v2, "_wait_for_app_server", lambda port, timeout: True)

        def start_interposer(*args, **kwargs):
            interposer_args["args"] = args
            interposer_args["kwargs"] = kwargs
            return 41002, lambda: stopped.append(True)

        monkeypatch.setattr(codex_interposer, "start_interposer_thread", start_interposer)

        def render_overlay(*args, **kwargs):
            overlay = codex.render_overlay(*args, **kwargs)
            overlay["mcp_servers"] = {"other-server": {"command": "other-helper"}}
            return overlay

        with pytest.raises(SystemExit) as exc:
            v2.launch_codex(
                {
                    "workspace": WS,
                    "profile": "myprof",
                    "codex_models": ["system.ai.gpt-5-6-sol"],
                    "oss_models": ["system.ai.glm-5-2"],
                    "_codex_launch_provider": "provider-a",
                },
                ["--search", *model_args],
                binary="codex",
                start_model="gpt-start",
                render_overlay=render_overlay,
            )

        assert exc.value.code == 7
        assert startup_check_sent.wait(3) is not omnigent
        validation_body = {
            "route_options": [
                {"model": "system.ai.gpt-5-6-sol", "harness": "codex"},
                {"model": "system.ai.glm-5-2", "harness": "codex"},
            ],
            "route_selector": {"router_name": v2.routing.configured_router_name()},
        }
        provider_header = {v2.route_validation.MODEL_PROVIDER_SERVICE_HEADER: "provider-a"}
        assert validation_requests == (
            [] if omnigent else [("token-1", validation_body, provider_header)]
        )
        assert processes[0].argv[:7] == [
            "codex",
            "app-server",
            "--config",
            'model_provider="Databricks"',
            "--config",
            'model="gpt-start"',
            "--config",
        ]
        assert processes[0].argv[7].startswith("model_providers.Databricks={")
        hook_override = next(
            arg for arg in processes[0].argv if arg.startswith("hooks.PreToolUse=[{")
        )
        assert 'matcher = "Agent|.*spawn_agent$"' in hook_override
        assert "codex-router-hook route-subagent" in hook_override
        assert f"--host {WS}" in hook_override
        assert "--profile myprof" in hook_override
        assert "--model system.ai.gpt-5-6-sol" in hook_override
        assert "--model system.ai.glm-5-2" in hook_override
        config_values = processes[0].argv[3:-2:2]
        config = tomllib.loads("\n".join(config_values))
        assert config["mcp_servers"]["other-server"] == {"command": "other-helper"}
        assert config["responses_api_metadata"]["smart_router_recipe_name"] == (
            v2.routing.configured_router_name()
        )
        native_overrides = config["features"]["multi_agent_v2"]
        assert native_overrides["non_code_mode_only"] is False
        assert "features.multi_agent_v2.non_code_mode_only=false" in config_values
        for key, role in (
            ("root_agent_usage_hint_text", "You are /root,"),
            ("subagent_usage_hint_text", "You are a child agent"),
        ):
            prefix = f"features.multi_agent_v2.{key}="
            hint = json.loads(
                next(arg.removeprefix(prefix) for arg in config_values if arg.startswith(prefix))
            )
            assert hint.startswith(role)
            assert v2.CODEX_V2_COLLABORATION_GUIDANCE in hint
            assert native_overrides[key] == hint
        assert user_config.read_bytes() == saved_config
        assert (
            "shell_environment_policy.set.UCODE_SESSION_ENV_FILE="
            + json.dumps(os.environ["UCODE_SESSION_ENV_FILE"])
        ) in config_values
        assert (
            "shell_environment_policy.set.UCODE_SMART_ROUTER_PYTHON="
            + json.dumps(os.environ["UCODE_SMART_ROUTER_PYTHON"])
        ) in config_values
        assert processes[0].argv[-2:] == [
            "--listen",
            "ws://127.0.0.1:41001",
        ]
        assert processes[0].kwargs["env"][v2.OAUTH_TOKEN_ENV_VAR] == "token-1"
        assert processes[0].kwargs["env"]["CODEX_HOME"] == str(tmp_path)
        tui_argv = processes[1].argv
        expected_tui_args = [
            "--remote",
            "ws://127.0.0.1:41002",
            *([] if model_args else ["--model", "gpt-start"]),
            "--search",
            *model_args,
        ]
        statusline_args = [
            "--config",
            "features.tui_extensions_statusline=true",
            "--config",
            'tui.status_line_provider={server = "ug-smart-router-statusline", tool = "statusline"}',
        ]
        feature_args = [
            arg
            for value in config_values
            if value.startswith("features.")
            for arg in ("--config", value)
        ]
        expected_tui_args = [*feature_args, *statusline_args[2:], *expected_tui_args]
        assert statusline_args[1] in config_values
        assert statusline_args[3] in config_values
        server_config = next(
            value
            for value in config_values
            if value.startswith("mcp_servers.ug-smart-router-statusline=")
        )
        assert json.dumps(sys.executable) in server_config
        assert (
            'args = ["-P", "-X", "utf8", "-m", "ucode.smart_routing.codex_statusline"]'
            in server_config
        )
        assert json.dumps(os.environ["UCODE_SESSION_ENV_FILE"]) in server_config
        if platform_name == "nt":
            assert tui_argv[:4] == [
                "codex",
                "--config",
                'model_provider="Databricks"',
                "--config",
            ]
            assert tui_argv[4] == processes[0].argv[7]
            assert tui_argv[5:] == expected_tui_args
        else:
            assert tui_argv == ["codex", *expected_tui_args]
        tui_config = tomllib.loads(
            "\n".join(
                tui_argv[index + 1] for index, arg in enumerate(tui_argv) if arg == "--config"
            )
        )
        assert tui_config["features"] == config["features"]
        assert not any(arg.startswith("hooks.") for arg in tui_argv)
        assert not any(arg.startswith("mcp_servers.") for arg in tui_argv)
        assert user_config.read_bytes() == saved_config
        assert interposer_args["args"] == (v2.LOOPBACK_HOST, "ws://127.0.0.1:41001")
        assert interposer_args["kwargs"]["available_models"] == [
            "system.ai.gpt-5-6-sol",
            "system.ai.glm-5-2",
        ]
        assert interposer_args["kwargs"]["session_controls"] == Path(
            os.environ["UCODE_SESSION_ENV_FILE"]
        )
        assert interposer_args["kwargs"]["workspace"] == (WS if first_prompt else None)
        assert token_calls == [(WS, "myprof")]
        if first_prompt:
            assert interposer_args["kwargs"]["token_provider"]() == "token-2"
            assert token_calls == [(WS, "myprof"), (WS, "myprof")]
        else:
            assert interposer_args["kwargs"]["token_provider"] is None
        assert os.environ[v2.ENABLE_SUBAGENT_ROUTING_ENV_VAR] == ("0" if first_prompt else "1")
        assert os.environ[v2.OAUTH_TOKEN_ENV_VAR] == "token-1"
        assert interposer_args["kwargs"]["switch_message_fn"] is v2.format_routing_notice
        if omnigent:
            assert interposer_args["kwargs"]["validate_model_list"] is None
            assert validation_requests == []
        else:
            assert interposer_args["kwargs"]["validate_model_list"]() == {"routing_disabled": False}
            assert validation_requests[1:] == [
                (f"token-{len(token_calls)}", validation_body, provider_header)
            ]
        assert stopped == [True]
        assert processes[0].terminated is True

    @pytest.mark.parametrize("platform_name", ["posix", "nt"])
    @pytest.mark.parametrize("managed_model", [None, "admin-model"])
    def test_app_server_launch_runs_the_given_command_with_routing_settings(
        self, monkeypatch, managed_model, platform_name
    ):
        launches = []
        validations = []
        # Windows waits on the spawned app-server, so only there does the background check survive.
        monkeypatch.setattr(v2, "os", SimpleNamespace(name=platform_name, environ=os.environ))
        monkeypatch.setattr(codex, "ug_version", lambda: "0.1.0")
        monkeypatch.setattr(codex, "agent_version", lambda binary: "0.148.0")
        monkeypatch.setattr(custom_oauth, "get_databricks_token", lambda *_args, **_kw: "token")
        monkeypatch.setattr(
            v2.subprocess, "Popen", lambda *_args, **_kw: pytest.fail("started another process")
        )
        monkeypatch.setattr(
            v2.route_validation, "start", lambda *args, **_kw: validations.append(args)
        )

        def hand_off(argv):
            launches.append(argv)
            raise SystemExit(0)

        monkeypatch.setattr(v2, "exec_or_spawn", hand_off)
        app_server_args = [
            "-c",
            'model="gpt-5-5"',
            "app-server",
            "--listen",
            "ws://127.0.0.1:4310",
        ]
        state = {"workspace": WS, "profile": "myprof", "codex_models": ["system.ai.gpt-5-6-sol"]}
        if managed_model:
            state["codex_default_model"] = managed_model

        with pytest.raises(SystemExit):
            v2.launch_codex(
                state,
                app_server_args,
                binary="codex",
                start_model="gpt-start",
                render_overlay=codex.render_overlay,
            )

        [argv] = launches
        assert argv[0] == "codex"
        assert argv[-len(app_server_args) :] == app_server_args
        routing_args = argv[1 : -len(app_server_args)]
        assert set(routing_args[0::2]) == {"--config"}
        config = tomllib.loads("\n".join(routing_args[1::2]))
        # Only the managed default, which the caller's later `-c model` overrides; never ug's pick.
        assert config.get("model") == managed_model
        assert config["model_provider"] == "Databricks"
        assert config["features"]["hooks"] is True
        [spawn_hook] = config["hooks"]["PreToolUse"]
        assert "codex-router-hook route-subagent" in spawn_hook["hooks"][0]["command"]
        assert config["responses_api_metadata"]["smart_router_recipe_name"] == (
            v2.routing.configured_router_name()
        )
        assert config["shell_environment_policy"]["set"] == {
            "UCODE_SESSION_ENV_FILE": os.environ["UCODE_SESSION_ENV_FILE"],
            "UCODE_SMART_ROUTER_PYTHON": os.environ["UCODE_SMART_ROUTER_PYTHON"],
        }
        assert os.environ[v2.OAUTH_TOKEN_ENV_VAR] == "token"
        assert len(validations) == (platform_name == "nt")

    def _run_app_server_launch_case(
        self,
        tmp_path,
        monkeypatch,
        native_catalog,
        *,
        source_model,
        catalog_source,
        launch_source,
        failure=None,
        rendered_overlay=False,
    ):
        launches = []
        token_calls = []
        empty_discovery = source_model is None and catalog_source is None and failure is None
        if empty_discovery:
            monkeypatch.setattr(codex_catalog, "prepare_codex_catalog", native_catalog)
            monkeypatch.setattr(
                codex_catalog,
                "_run_catalog_command",
                lambda *_args: subprocess.CompletedProcess(
                    [],
                    0,
                    json.dumps(
                        {"models": [{"slug": name} for name in ("gpt-start", "gpt-alternative")]}
                    ),
                ),
            )

        def get_token(*_args, **_kwargs):
            token_calls.append(True)
            return "token"

        monkeypatch.setattr(custom_oauth, "get_databricks_token", get_token)

        catalogs = {}
        for name in (catalog_source, launch_source):
            if name and name not in catalogs:
                path = tmp_path / f"{name}.json"
                path.write_text(
                    json.dumps({"models": [{"slug": f"{name}-model", "source": name}]}),
                    encoding="utf-8",
                )
                catalogs[name] = path
        monkeypatch.setattr(v2, "custom_catalog_path", lambda: catalogs.get(catalog_source))
        session_env_path = tmp_path / "session.env"
        monkeypatch.setattr(v2, "_prepare_smart_router_session", lambda _agent: session_env_path)
        monkeypatch.setenv(v2.SESSION_PYTHON_ENV_VAR, "python")

        warnings = []
        monkeypatch.setattr(v2, "print_warning", warnings.append)
        builder_calls = []
        if failure == "builder":

            def fail_builder(binary, names, *, workspace, get_token, include_bundled):
                builder_calls.append((workspace, get_token()))
                raise RuntimeError("catalog builder failed")

            monkeypatch.setattr(codex_catalog, "prepare_codex_catalog", fail_builder)
        elif failure == "write":

            def fail_write(*_args, **_kwargs):
                raise OSError("catalog write failed")

            monkeypatch.setattr(v2, "write_json_file", fail_write)

        def hand_off(argv):
            launches.append(argv)
            raise SystemExit(0)

        monkeypatch.setattr(v2, "exec_or_spawn", hand_off)
        launch_config = {"model_provider": "Databricks"}
        if source_model:
            launch_config["model"] = source_model
        if launch_source:
            launch_config["model_catalog_json"] = str(catalogs[launch_source])

        with pytest.raises(SystemExit) as exc:
            v2.launch_codex(
                {"workspace": WS},
                ["app-server"],
                binary="codex",
                start_model="gpt-start",
                render_overlay=lambda *_args, **_kw: (
                    dict(launch_config)
                    if rendered_overlay
                    else pytest.fail("rendered the default overlay")
                ),
                launch_config=None if rendered_overlay else launch_config,
                available_models=[] if empty_discovery else ["mps-primary"],
            )

        assert exc.value.code == 0
        [argv] = launches
        assert argv[-1] == "app-server"
        config = tomllib.loads("\n".join(argv[1:-1][1::2]))
        assert config.get("model") == source_model
        return {
            "builder_calls": builder_calls,
            "catalogs": catalogs,
            "config": config,
            "empty_discovery": empty_discovery,
            "output": session_env_path.with_suffix(".models.json"),
            "token_calls": token_calls,
            "warnings": warnings,
        }

    @pytest.mark.parametrize(
        ("source_model", "catalog_source", "launch_source", "expected_source"),
        [
            pytest.param(None, None, None, None, id="default-model"),
            pytest.param("mps-primary", None, None, None, id="explicit-model"),
            pytest.param(None, "user", None, "user", id="user-catalog"),
            pytest.param(None, "managed", "explicit", "explicit", id="launch-catalog-wins"),
        ],
    )
    def test_app_server_launch_catalog_precedence(
        self,
        tmp_path,
        monkeypatch,
        native_catalog,
        source_model,
        catalog_source,
        launch_source,
        expected_source,
    ):
        result = self._run_app_server_launch_case(
            tmp_path,
            monkeypatch,
            native_catalog,
            source_model=source_model,
            catalog_source=catalog_source,
            launch_source=launch_source,
        )

        if expected_source:
            assert Path(result["config"]["model_catalog_json"]) == result["output"]
            assert json.loads(result["output"].read_text(encoding="utf-8")) == {
                "models": [
                    {
                        "slug": f"{expected_source}-model",
                        "source": expected_source,
                        "multi_agent_tools_in_code_mode": True,
                    }
                ]
            }
            assert json.loads(result["catalogs"][expected_source].read_text(encoding="utf-8")) == {
                "models": [{"slug": f"{expected_source}-model", "source": expected_source}]
            }

        if result["empty_discovery"]:
            assert Path(result["config"]["model_catalog_json"]) == result["output"]
            assert json.loads(result["output"].read_text(encoding="utf-8")) == {
                "models": [
                    {"slug": name, "multi_agent_tools_in_code_mode": True}
                    for name in ("gpt-start", "gpt-alternative")
                ]
            }
            assert len(result["warnings"]) == 1
            assert "model metadata is unavailable" in result["warnings"][0]
        else:
            assert result["warnings"] == []

    def test_app_server_launch_builder_failure_keeps_launch_config(
        self, tmp_path, monkeypatch, native_catalog
    ):
        result = self._run_app_server_launch_case(
            tmp_path,
            monkeypatch,
            native_catalog,
            source_model=None,
            catalog_source=None,
            launch_source=None,
            failure="builder",
        )

        assert "model_catalog_json" not in result["config"]
        assert not result["output"].exists()
        assert result["token_calls"] == [True]
        assert result["builder_calls"] == [(WS, "token")]
        assert len(result["warnings"]) == 1
        assert "Could not enable plaintext Codex task routing" in result["warnings"][0]
        assert "catalog builder failed" in result["warnings"][0]

    @pytest.mark.parametrize(
        ("catalog_source", "launch_source", "rendered_overlay", "expected_source"),
        [
            pytest.param("managed", "explicit", False, "explicit", id="launch-catalog"),
            pytest.param("user", None, False, "user", id="configured-catalog"),
            pytest.param("user", None, True, "user", id="rendered-catalog"),
        ],
    )
    def test_app_server_launch_write_failure_keeps_source_catalog(
        self,
        tmp_path,
        monkeypatch,
        native_catalog,
        catalog_source,
        launch_source,
        rendered_overlay,
        expected_source,
    ):
        result = self._run_app_server_launch_case(
            tmp_path,
            monkeypatch,
            native_catalog,
            source_model=None,
            catalog_source=catalog_source,
            launch_source=launch_source,
            failure="write",
            rendered_overlay=rendered_overlay,
        )

        assert result["config"]["model_catalog_json"] == str(result["catalogs"][expected_source])
        assert not result["output"].exists()
        assert json.loads(result["catalogs"][expected_source].read_text(encoding="utf-8")) == {
            "models": [{"slug": f"{expected_source}-model", "source": expected_source}]
        }
        assert len(result["warnings"]) == 1
        assert "Could not enable plaintext Codex task routing" in result["warnings"][0]
        assert "catalog write failed" in result["warnings"][0]

    def test_managed_http_headers_reach_app_server_config(self, monkeypatch):
        # Smart routing rebuilds the overlay and passes it to the app-server as `-c` overrides that
        # replace the whole provider block, so the admin headers must be threaded through here too —
        # otherwise they are written to config.toml but stripped from the launched inference calls.
        processes = []
        monkeypatch.setenv(v2.ENABLE_SMART_ROUTING_ENV_VAR, "1")
        monkeypatch.setenv("CODEX_HOME", "/user/codex-home")
        monkeypatch.setattr(codex, "ug_version", lambda: "0.1.0")
        monkeypatch.setattr(codex, "agent_version", lambda binary: "0.148.0")

        class FakeProcess:
            def __init__(self, argv, **kwargs):
                self.argv = argv
                processes.append(self)

            def wait(self, timeout=None):
                return 0

            def terminate(self):
                pass

            def send_signal(self, _signal):
                pass

        monkeypatch.setattr(v2.subprocess, "Popen", FakeProcess)
        monkeypatch.setattr(custom_oauth, "get_databricks_token", lambda *_a, **_k: "token")
        monkeypatch.setattr(v2, "_free_port", lambda: 41001)
        monkeypatch.setattr(v2, "_wait_for_app_server", lambda port, timeout: True)
        monkeypatch.setattr(
            codex_interposer,
            "start_interposer_thread",
            lambda *_a, **_k: (41002, lambda: None),
        )

        with pytest.raises(SystemExit):
            v2.launch_codex(
                {
                    "workspace": WS,
                    "codex_models": ["system.ai.gpt-5-6-sol"],
                    "codex_http_headers": {"x-databricks-workspace": "eng-ml-inference"},
                },
                [],
                binary="codex",
                start_model="gpt-start",
                render_overlay=codex.render_overlay,
            )

        provider_arg = next(
            arg for arg in processes[0].argv if arg.startswith("model_providers.Databricks=")
        )
        assert "x-databricks-workspace" in provider_arg
        assert "eng-ml-inference" in provider_arg

    def test_v2_pre_tool_hook_leaves_saved_hooks_to_codex(self, tmp_path, monkeypatch):
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text(
            "[[hooks.PreToolUse]]\n"
            'matcher = "Bash"\n'
            "[[hooks.PreToolUse.hooks]]\n"
            'type = "command"\n'
            'command = "user-policy"\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("CODEX_HOME", str(codex_home))

        before = (codex_home / "config.toml").read_bytes()
        configured = v2._v2_hooks(
            {"workspace": WS, "profile": "myprof"},
            ["system.ai.gpt-5-6-sol"],
        )["PreToolUse"]

        assert len(configured) == 1
        assert configured[0]["matcher"] == "Agent|.*spawn_agent$"
        assert "--model system.ai.gpt-5-6-sol" in configured[0]["hooks"][0]["command"]
        assert (codex_home / "config.toml").read_bytes() == before

    def test_v2_pre_tool_hook_uses_current_model(self, tmp_path, monkeypatch):
        monkeypatch.setattr("ucode.databricks.ug_binary", lambda: "/bin/ug")
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text(
            "[[hooks.PreToolUse]]\n"
            'matcher = "Agent|.*spawn_agent$"\n'
            "[[hooks.PreToolUse.hooks]]\n"
            'type = "command"\n'
            'command = "ucode codex-router-hook route-subagent --model old"\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("CODEX_HOME", str(codex_home))

        configured = v2._v2_hooks(
            {"workspace": WS, "profile": "myprof"},
            ["system.ai.gpt-5-6-sol"],
        )["PreToolUse"]

        routing_commands = [
            hook["command"]
            for group in configured
            for hook in group["hooks"]
            if "codex-router-hook" in hook["command"]
        ]
        assert len(routing_commands) == 1
        assert routing_commands[0].startswith("/bin/ug codex-router-hook route-subagent ")
        assert "--model system.ai.gpt-5-6-sol" in routing_commands[0]
        assert "--model old" not in routing_commands[0]

    def test_missing_cached_models_starts_with_bootstrap_model(self, monkeypatch):
        monkeypatch.setenv(v2.ENABLE_SMART_ROUTING_ENV_VAR, "1")
        monkeypatch.setattr(
            custom_oauth, "get_databricks_token", lambda workspace, profile, **_kw: "token"
        )
        monkeypatch.setattr(codex, "agent_version", lambda binary: "unknown")
        monkeypatch.setattr(v2, "_free_port", lambda: 41001)
        monkeypatch.setattr(v2, "_wait_for_app_server", lambda port, timeout: True)
        monkeypatch.setattr(
            v2.subprocess,
            "Popen",
            lambda *args, **kwargs: type(
                "Process",
                (),
                {
                    "wait": lambda self, timeout=None: 0,
                    "terminate": lambda self: None,
                    "kill": lambda self: None,
                },
            )(),
        )
        monkeypatch.setattr(
            codex_interposer,
            "start_interposer_thread",
            lambda *args, **kwargs: (41002, lambda: None),
        )

        with pytest.raises(SystemExit) as exc:
            v2.launch_codex(
                {"workspace": WS},
                [],
                binary="codex",
                start_model="gpt-5.6-luna",
                render_overlay=codex.render_overlay,
            )

        assert exc.value.code == 0


class TestCustomCatalogModels:
    def _catalog(self, path, slugs):
        path.write_text(
            json.dumps({"models": [{"slug": slug, "visibility": "list"} for slug in slugs]}),
            encoding="utf-8",
        )
        return path

    def _settings(self, tmp_path, monkeypatch, *, managed=None, cli=None, default=None):
        home = tmp_path / "codex-home"
        home.mkdir()
        monkeypatch.setenv("CODEX_HOME", str(home))
        managed_path = tmp_path / "managed_config.toml"
        cli_path = home / "ucode.config.toml"
        default_path = home / "config.toml"
        for path, catalog in (
            (managed_path, managed),
            (cli_path, cli),
            (default_path, default),
        ):
            if catalog:
                text = "model_catalog_json = " + json.dumps(str(catalog)) + "\n"
            else:
                text = 'model = "gpt-5"\n'
            path.write_text(text, encoding="utf-8")
        monkeypatch.setattr(codex_config, "codex_managed_config_path", lambda: managed_path)
        monkeypatch.setattr(codex_config, "DEFAULT_CODEX_CONFIG_PATH", cli_path)

    @pytest.mark.parametrize(
        ("managed_catalog", "cli_catalog", "default_catalog", "expected"),
        [
            ("gpt-managed", "gpt-cli", "gpt-default", ["gpt-managed"]),
            (None, "gpt-cli", "gpt-default", ["gpt-cli"]),
            (None, None, "gpt-default", ["gpt-default"]),
            (None, None, None, None),
        ],
    )
    def test_config_precedence(
        self,
        tmp_path,
        monkeypatch,
        managed_catalog,
        cli_catalog,
        default_catalog,
        expected,
    ):
        managed, cli, default = (
            self._catalog(tmp_path / f"{name}.json", [slug]) if slug else None
            for name, slug in (
                ("managed", managed_catalog),
                ("cli", cli_catalog),
                ("default", default_catalog),
            )
        )
        self._settings(tmp_path, monkeypatch, managed=managed, cli=cli, default=default)

        assert codex_config.custom_catalog_models() == expected

    def test_catalog_path_uses_config_precedence(self, tmp_path, monkeypatch):
        managed = self._catalog(tmp_path / "managed.json", ["gpt-managed"])
        cli = self._catalog(tmp_path / "cli.json", ["gpt-cli"])
        default = self._catalog(tmp_path / "default.json", ["gpt-default"])
        self._settings(tmp_path, monkeypatch, managed=managed, cli=cli, default=default)

        assert codex_config.custom_catalog_path() == managed

    def test_unreadable_catalog_warns_and_falls_back(self, tmp_path, monkeypatch):
        self._settings(tmp_path, monkeypatch, cli=tmp_path / "missing.json")
        warnings = []
        monkeypatch.setattr(codex_config, "print_warning", warnings.append)

        assert codex_config.custom_catalog_models() is None
        assert len(warnings) == 1
        assert "falling back to the cached model services" in warnings[0]

    def _catalog_with_visibility(self, path, rows):
        path.write_text(
            json.dumps({"models": [{"slug": slug, "visibility": vis} for slug, vis in rows]}),
            encoding="utf-8",
        )
        return path

    def test_only_visible_models_offered_to_router(self, tmp_path, monkeypatch):
        catalog = self._catalog_with_visibility(
            tmp_path / "cli.json",
            [
                ("system.ai.glm-5-3", "list"),
                ("glm-5-3", "hide"),
                ("system.ai.gpt-5-6-luna", "list"),
                ("gpt-5.6-luna", "hide"),
                ("gpt-5-6-luna", "hide"),
            ],
        )
        self._settings(tmp_path, monkeypatch, cli=catalog)

        assert codex_config.custom_catalog_models() == [
            "system.ai.glm-5-3",
            "system.ai.gpt-5-6-luna",
        ]

    @pytest.mark.parametrize("write_fails", [False, True])
    def test_launch_prefers_catalog_over_cached_models(
        self, tmp_path, monkeypatch, capsys, write_fails
    ):
        self._settings(
            tmp_path,
            monkeypatch,
            cli=self._catalog(tmp_path / "cli.json", ["gpt-6-astra", "gpt-6-b"]),
        )
        monkeypatch.setenv(v2.ENABLE_SMART_ROUTING_ENV_VAR, "1")
        monkeypatch.setattr(custom_oauth, "get_databricks_token", lambda *_args, **_kw: "token")
        monkeypatch.setattr(v2, "_free_port", lambda: 41001)
        monkeypatch.setattr(v2, "_wait_for_app_server", lambda port, timeout: True)
        monkeypatch.setattr(codex, "agent_version", lambda _binary: "0.156.0")
        monkeypatch.setattr(codex, "ug_version", lambda: "test")
        launched = []
        warnings = []
        monkeypatch.setattr(v2, "print_warning", warnings.append)
        if write_fails:

            def fail_write(*_args, **_kwargs):
                raise OSError("catalog write failed")

            monkeypatch.setattr(v2, "write_json_file", fail_write)

        class FakeProcess:
            def __init__(self, argv, **kwargs):
                launched.append(argv)

            def wait(self, timeout=None):
                return 0

            def terminate(self):
                pass

            def kill(self):
                pass

        monkeypatch.setattr(v2.subprocess, "Popen", FakeProcess)
        interposer_kwargs = {}

        def start_interposer(*args, **kwargs):
            interposer_kwargs.update(kwargs)
            return 41002, lambda: None

        monkeypatch.setattr(codex_interposer, "start_interposer_thread", start_interposer)

        with pytest.raises(SystemExit):
            v2.launch_codex(
                {"workspace": WS, "codex_models": ["system.ai.gpt-5-6-sol"]},
                [],
                binary="codex",
                start_model="gpt-6-astra",
                render_overlay=codex.render_overlay,
            )

        assert interposer_kwargs["available_models"] == ["gpt-6-astra", "gpt-6-b"]
        catalog_override = next(arg for arg in launched[0] if arg.startswith("model_catalog_json="))
        session_catalog = Path(json.loads(catalog_override.split("=", 1)[1]))
        source_catalog = tmp_path / "cli.json"
        if write_fails:
            assert session_catalog == source_catalog
            assert len(warnings) == 1
            assert "catalog write failed" in warnings[0]
        else:
            assert session_catalog != source_catalog
            expected = json.loads(source_catalog.read_text())
            for model in expected["models"]:
                model["multi_agent_tools_in_code_mode"] = True
            assert json.loads(session_catalog.read_text()) == expected
            assert warnings == []
        assert all(
            "multi_agent_tools_in_code_mode" not in row
            for row in json.loads(source_catalog.read_text())["models"]
        )
        hook_override = next(arg for arg in launched[0] if arg.startswith("hooks.PreToolUse="))
        assert "--model gpt-6-astra" in hook_override
        assert "--model gpt-6-b" in hook_override
        assert "gpt-5-6-sol" not in hook_override
        assert "Smart routing:" not in capsys.readouterr().out

    def test_start_model_comes_from_custom_catalog(self, monkeypatch):
        calls = []
        monkeypatch.setattr(codex, "agent_version", lambda _binary: "0.171.0")
        monkeypatch.setenv(v2.ENABLE_SMART_ROUTING_ENV_VAR, "1")
        monkeypatch.setattr(codex, "clear_model_preferences", lambda state: False)
        monkeypatch.setattr(codex, "_smart_routing_config_model", lambda state: None)
        monkeypatch.setattr(codex, "custom_catalog_models", lambda: ["gpt-6-astra", "gpt-6-b"])

        def launch_v2(state, tool_args, **kwargs):
            calls.append(kwargs)
            raise SystemExit(0)

        monkeypatch.setattr(v2, "launch_codex", launch_v2)

        with pytest.raises(SystemExit):
            codex.launch(
                {"workspace": WS, "codex_models": ["system.ai.gpt-5-6-luna"]},
                [],
                options=LaunchOptions(launch_smart_routing=True),
            )

        assert calls[0]["start_model"] == "gpt-6-astra"


def test_interposer_startup_failure_is_propagated(monkeypatch):
    async def fail_to_serve(*args, **kwargs):
        raise OSError("bind failed")

    monkeypatch.setattr(codex_interposer, "_serve", fail_to_serve)

    with pytest.raises(RuntimeError, match="failed to start") as exc:
        codex_interposer.start_interposer_thread(
            v2.LOOPBACK_HOST,
            "ws://127.0.0.1:41001",
            "model-x",
        )

    assert isinstance(exc.value.__cause__, OSError)


class TestInterposerSession:
    def _turn_start(self, model: str, thread_id: str = "t1", prompt: str = "Fix the parser") -> str:
        return json.dumps(
            {
                "method": codex_interposer.TURN_START,
                "id": 1,
                "params": {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": prompt}],
                    "model": model,
                },
            }
        )

    def test_switches_first_turn(self):
        sess = codex_interposer._Session("gpt-5.5", log=lambda _m: None)
        result = sess.on_tui_frame(self._turn_start("system.ai.gpt-5-6-luna"))
        assert json.loads(result.frame)["params"]["model"] == "gpt-5.5"
        assert result.needs_settings_update

    def test_does_not_schedule_notification_when_model_is_already_selected(self):
        sess = codex_interposer._Session("gpt-5.5", log=lambda _m: None)
        frame = self._turn_start("gpt-5.5")
        assert sess.on_tui_frame(frame) == codex_interposer.TuiFrameResult(
            frame, needs_settings_update=False
        )
        assert sess.on_engine_frame(self._turn_started("turn-1")) == []
        later_selection = self._turn_start("gpt-5.6")
        assert sess.on_tui_frame(later_selection) == codex_interposer.TuiFrameResult(
            later_selection, needs_settings_update=False
        )

    def test_non_turn_frames_pass_through(self):
        sess = codex_interposer._Session("gpt-5.5", log=lambda _m: None)
        frame = json.dumps({"method": "initialize", "id": 1, "params": {}})
        assert sess.on_tui_frame(frame) == codex_interposer.TuiFrameResult(
            frame, needs_settings_update=False
        )

    def _turn_started(self, turn_id: str, thread_id: str = "t1") -> str:
        return json.dumps(
            {
                "method": codex_interposer.TURN_STARTED,
                "params": {"threadId": thread_id, "turn": {"id": turn_id}},
            }
        )

    def test_injects_note_when_switched_turn_starts(self):
        sess = codex_interposer._Session("gpt-5.5", log=lambda _m: None)
        sess.on_tui_frame(self._turn_start("luna"))
        injected = sess.on_engine_frame(self._turn_started("turn-1"))
        settings = next(m for m in injected if m["method"] == codex_interposer.SETTINGS_UPDATED)
        assert settings["params"]["threadId"] == "t1"
        assert settings["params"]["threadSettings"]["model"] == "gpt-5.5"

    def test_injects_switch_note_as_agent_message_when_message_set(self):
        sess = codex_interposer._Session(
            "gpt-5.5", log=lambda _m: None, switch_message="selected glm-5-2 because X"
        )
        sess.on_tui_frame(self._turn_start("luna"))
        injected = sess.on_engine_frame(self._turn_started("turn-1"))
        started = next(m for m in injected if m["method"] == codex_interposer.ITEM_STARTED)
        completed = next(m for m in injected if m["method"] == codex_interposer.ITEM_COMPLETED)
        assert started["params"]["turnId"] == "turn-1"
        assert completed["params"]["turnId"] == "turn-1"
        for frame in (started, completed):
            item = frame["params"]["item"]
            assert item["type"] == "agentMessage"
            assert item["text"] == "selected glm-5-2 because X"
        assert started["params"]["item"]["id"] == completed["params"]["item"]["id"]

    def test_no_note_without_message(self):
        sess = codex_interposer._Session("gpt-5.5", log=lambda _m: None)
        sess.on_tui_frame(self._turn_start("luna"))
        injected = sess.on_engine_frame(self._turn_started("turn-1"))
        assert [m["method"] for m in injected] == [codex_interposer.SETTINGS_UPDATED]

    def test_routes_only_first_turn_and_preserves_later_model_selection(self):
        sess = codex_interposer._Session("gpt-5.5", log=lambda _m: None)
        sess.on_tui_frame(self._turn_start("luna"))
        assert sess.on_engine_frame(self._turn_started("turn-1"))
        second_turn = self._turn_start("luna")
        assert sess.on_tui_frame(second_turn) == codex_interposer.TuiFrameResult(
            second_turn, needs_settings_update=False
        )
        assert sess.on_engine_frame(self._turn_started("turn-2")) == []

    def test_routes_first_prompt_and_uses_returned_model_and_rationale(self):
        calls = []

        def select(prompt):
            calls.append(prompt)
            return (
                codex_interposer.routing.RoutingDecision(
                    model="claude-opus-4-8",
                    rationale="Task classified as bugfix.",
                ),
                None,
            )

        sess = codex_interposer._Session(
            None,
            log=lambda _m: None,
            available_models=["claude-opus-4-8", "gpt-5.5"],
            route_decision=select,
            switch_message_fn=v2.format_routing_notice,
        )

        result = sess.on_tui_frame(self._turn_start("gpt-5.5", prompt="Fix issue #42"))

        assert calls == ["Fix issue #42"]
        assert json.loads(result.frame)["params"]["model"] == "claude-opus-4-8"
        assert result.needs_settings_update
        assert "Task classified as bugfix." in sess.switch_message

    def test_maps_selected_uc_gpt_model_and_shows_routing_notice(self):
        def select(_prompt):
            return (
                codex_interposer.routing.RoutingDecision(
                    model="system.ai.gpt-5-6-luna",
                    rationale="Trivial task.",
                ),
                None,
            )

        sess = codex_interposer._Session(
            None,
            log=lambda _m: None,
            route_decision=select,
            switch_message_fn=v2._switch_message,
        )
        frame = self._turn_start("system.ai.gpt-5-6-luna")

        result = sess.on_tui_frame(frame)
        assert json.loads(result.frame)["params"]["model"] == "gpt-5.6-luna"
        injected = sess.on_engine_frame(self._turn_started("turn-1"))

        assert [message["method"] for message in injected] == [
            codex_interposer.SETTINGS_UPDATED,
            codex_interposer.ITEM_STARTED,
            codex_interposer.ITEM_COMPLETED,
        ]
        assert "Selected Model : gpt-5.6-luna" in (injected[1]["params"]["item"]["text"])

    def test_routes_first_prompt_to_oss_model(self):
        def select(_prompt):
            return (
                codex_interposer.routing.RoutingDecision(
                    model="system.ai.glm-5-2",
                    rationale="Short isolated task.",
                ),
                None,
            )

        sess = codex_interposer._Session(
            None,
            log=lambda _m: None,
            available_models=["system.ai.gpt-5-6-sol", "system.ai.glm-5-2"],
            route_decision=select,
            switch_message_fn=v2._switch_message,
        )

        result = sess.on_tui_frame(self._turn_start("system.ai.gpt-5-6-sol"))

        assert json.loads(result.frame)["params"]["model"] == "system.ai.glm-5-2"
        assert result.needs_settings_update
        assert "Selected Model : system.ai.glm-5-2" in sess.switch_message

    def test_router_failure_keeps_original_model(self):
        sess = codex_interposer._Session(
            None,
            log=lambda _m: None,
            route_decision=lambda prompt: (None, "router unavailable"),
        )
        frame = self._turn_start("gpt-start")

        assert sess.on_tui_frame(frame) == codex_interposer.TuiFrameResult(
            frame, needs_settings_update=False
        )

    def test_rewrites_nested_collaboration_mode_model(self):
        """The app-server re-derives the thread model from
        collaborationMode.settings.model on every turn/start, so the
        interposer must rewrite that nested field too — not just the
        top-level ``model`` field."""
        sess = codex_interposer._Session(
            None,
            log=lambda _m: None,
            route_decision=lambda _p: (
                codex_interposer.routing.RoutingDecision(
                    model="gpt-5.6-luna",
                    rationale="trivial",
                ),
                None,
            ),
            switch_message_fn=v2._switch_message,
        )
        frame = json.dumps(
            {
                "method": codex_interposer.TURN_START,
                "id": 1,
                "params": {
                    "threadId": "t1",
                    "input": [{"type": "text", "text": "hello"}],
                    "model": "gpt-6-astra",
                    "collaborationMode": {
                        "mode": "default",
                        "settings": {
                            "model": "gpt-6-astra",
                            "reasoning_effort": "high",
                        },
                    },
                },
            }
        )
        result = sess.on_tui_frame(frame)
        parsed = json.loads(result.frame)
        assert parsed["params"]["model"] == "gpt-5.6-luna"
        assert parsed["params"]["collaborationMode"]["settings"]["model"] == "gpt-5.6-luna"
        assert result.needs_settings_update


def test_routing_request_uses_models_prompt_and_same_token(monkeypatch):
    monkeypatch.delenv("SMART_ROUTER_NAME", raising=False)
    captured = {}
    logged = []

    def select_route(
        workspace, token, task, route_options, *, router_name, timeout, attribution_headers
    ):
        captured.update(
            workspace=workspace,
            token=token,
            task=task,
            route_options=list(route_options),
            router_name=router_name,
            timeout=timeout,
        )
        return (
            codex_interposer.routing.RoutingDecision(
                model="system.ai.gpt-5-6-sol",
                rationale="Bugfix needs deeper reasoning.",
            ),
            None,
        )

    monkeypatch.setattr(codex_routing.routing, "select_route", select_route)

    decision, reason = codex_routing.request_routing_decision(
        WS,
        "same-oauth-token",
        "Fix the parser",
        [
            "system.ai.kimi-k3-neo",
            "system.ai.gpt-5-6-sol",
            "system.ai.gpt-5-6-luna",
            "system.ai.glm-5-2",
        ],
        log=logged.append,
    )

    assert reason is None
    assert decision.model == "system.ai.gpt-5-6-sol"
    assert captured == {
        "workspace": WS,
        "token": "same-oauth-token",
        "task": "Fix the parser",
        "router_name": codex_routing.routing.ROUTER_NAME,
        "timeout": codex_routing.REQUEST_TIMEOUT_S,
        "route_options": [
            ("system.ai.kimi-k3-neo", "codex"),
            ("system.ai.gpt-5-6-sol", "codex"),
            ("system.ai.gpt-5-6-luna", "codex"),
            ("system.ai.glm-5-2", "codex"),
        ],
    }
    assert len(logged) == 1
    assert logged[0].startswith(f"[ROUTE] request POST {WS}/ai-gateway/routing/v1/routes:select: ")
    request_payload = json.loads(logged[0].split(": ", 1)[1])
    assert request_payload == {
        "route_options": [
            {"model": "system.ai.kimi-k3-neo", "harness": "codex"},
            {"model": "system.ai.gpt-5-6-sol", "harness": "codex"},
            {"model": "system.ai.gpt-5-6-luna", "harness": "codex"},
            {"model": "system.ai.glm-5-2", "harness": "codex"},
        ],
        "task": {"prompt": "Fix the parser"},
        "route_selector": {"router_name": codex_routing.routing.ROUTER_NAME},
    }
    assert "same-oauth-token" not in logged[0]


def test_routing_request_sends_each_model_id_once_as_given(monkeypatch):
    captured = {}

    def select_route(
        workspace, token, task, route_options, *, router_name, timeout, attribution_headers
    ):
        captured["route_options"] = list(route_options)
        return None, "not selected"

    monkeypatch.setattr(codex_routing.routing, "select_route", select_route)

    codex_routing.request_routing_decision(
        WS,
        "token",
        "Fix the parser",
        [
            "system.ai.gpt-5-6-sol",
            "gpt-5.6-sol",
            "system.ai.gpt-5-6-sol",
            "system.ai.gpt-5-6-luna",
        ],
    )

    assert captured["route_options"] == [
        ("system.ai.gpt-5-6-sol", "codex"),
        ("gpt-5.6-sol", "codex"),
        ("system.ai.gpt-5-6-luna", "codex"),
    ]
