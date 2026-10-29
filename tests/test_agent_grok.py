"""Tests for agents/grok.py."""

from __future__ import annotations

import contextlib
import json
from unittest.mock import MagicMock, patch

import pytest
import tomlkit

from ucode.agents import grok
from ucode.agents.args import LaunchOptions
from ucode.config_io import read_toml_safe, write_toml_file
from ucode.constants import MODEL_PROVIDER_SERVICE_HEADER
from ucode.os_compatibility.file_lock_cross_os import acquire_exclusive_file_lock, release_file_lock

WS = "https://example.databricks.com"

STATE = {
    "workspace": WS,
    "profile": "p",
    "claude_models": {
        "haiku": "system.ai.claude-haiku-5-5",
        "opus": "system.ai.claude-opus-5-5",
        "sonnet": "system.ai.claude-sonnet-5-5",
    },
    "codex_models": ["system.ai.gpt-5-5", "system.ai.gpt-oss-120b"],
    "gemini_models": ["system.ai.gemini-3-1-pro"],
    "oss_models": ["system.ai.glm-5-3", "system.ai.kimi-k3"],
}
# The per-family discovery lists, used when the model-services listing failed.
BUCKETS = dict(STATE)
# What `ug configure` saves from the listing: every Claude version and the Grok models too.
STATE = {
    **STATE,
    "grok_catalog": [
        "system.ai.grok-4-7",
        "system.ai.grok-4-6",
        "system.ai.claude-opus-5-5",
        "system.ai.claude-sonnet-5-5",
        "system.ai.claude-haiku-5-5",
        "system.ai.claude-haiku-4-5",
        "system.ai.gpt-5-5",
        "system.ai.glm-5-3",
        "system.ai.gpt-oss-120b",
        "system.ai.kimi-k3",
    ],
}


def _ids(catalog):
    return [model.id for model in catalog]


def _read_config() -> tomlkit.TOMLDocument:
    return tomlkit.parse(grok.GROK_CONFIG_PATH.read_text(encoding="utf-8"))


class TestSpec:
    def test_spec(self):
        assert grok.SPEC["binary"] == "grok"
        assert grok.SPEC["package"] == "@xai-official/grok"
        assert grok.SPEC["display"] == "Grok Build"
        assert grok.SPEC["config_path"] == grok.GROK_CONFIG_PATH


class TestDatabricksCatalog:
    def test_uses_the_saved_listing(self):
        assert _ids(grok.databricks_catalog(STATE)) == STATE["grok_catalog"]

    @pytest.mark.parametrize(
        "listing",
        [{}, {"grok_catalog": []}, {"grok_catalog": ["system.ai.gemini-3-1-pro"]}],
        ids=["failed", "empty", "nothing-usable"],
    )
    def test_falls_back_to_family_lists_without_a_usable_listing(self, listing):
        assert _ids(grok.databricks_catalog({**BUCKETS, **listing})) == [
            "system.ai.claude-opus-5-5",
            "system.ai.claude-sonnet-5-5",
            "system.ai.claude-haiku-5-5",
            "system.ai.gpt-5-5",
            "system.ai.glm-5-3",
            "system.ai.gpt-oss-120b",
            "system.ai.kimi-k3",
        ]

    def test_wires_and_routes(self):
        by_id = {m.id: m for m in grok.databricks_catalog(STATE)}
        # Grok models reject the `reasoning.summary` Grok sends on Responses.
        assert by_id["system.ai.grok-4-7"].wire == grok.WIRE_CHAT_COMPLETIONS
        assert by_id["system.ai.grok-4-7"].route == "/ai-gateway/mlflow/v1"
        assert by_id["system.ai.claude-opus-5-5"].wire == grok.WIRE_MESSAGES
        assert by_id["system.ai.claude-opus-5-5"].route == "/ai-gateway/anthropic/v1"
        assert by_id["system.ai.gpt-5-5"].wire == grok.WIRE_RESPONSES
        assert by_id["system.ai.gpt-5-5"].route == "/ai-gateway/mlflow/v1"
        # gpt-oss replies on Responses lack the usage details Grok requires.
        assert by_id["system.ai.gpt-oss-120b"].wire == grok.WIRE_CHAT_COMPLETIONS
        assert by_id["system.ai.glm-5-3"].wire == grok.WIRE_CHAT_COMPLETIONS
        assert by_id["system.ai.glm-5-3"].route == "/ai-gateway/mlflow/v1"

    def test_dedupes_and_ignores_malformed_entries(self):
        state = {
            "claude_models": {"opus": "system.ai.claude-opus-5-5", "sonnet": None},
            "codex_models": ["system.ai.gpt-5-5", "system.ai.gpt-5-5", 3, ""],
            "oss_models": "not-a-list",
        }
        assert _ids(grok.databricks_catalog(state)) == [
            "system.ai.claude-opus-5-5",
            "system.ai.gpt-5-5",
        ]

    def test_empty_without_discovery(self):
        assert grok.databricks_catalog({}) == []
        assert grok.databricks_catalog({"grok_catalog": []}) == []

    def test_catalog_from_listing_orders_and_filters(self):
        from ucode.databricks import ModelServiceEntry

        chat = ("mlflow/v1/chat/completions",)
        entries = [
            ModelServiceEntry("system.ai.gpt-5-4-nano", ("openai/v1/responses",)),
            ModelServiceEntry("system.ai.claude-haiku-4-5", ("anthropic/v1/messages",)),
            ModelServiceEntry("system.ai.grok-4-6", chat),
            ModelServiceEntry("system.ai.claude-opus-4-8", ("anthropic/v1/messages",)),
            ModelServiceEntry("system.ai.grok-4-7", chat),
            ModelServiceEntry("system.ai.claude-opus-5-5", ("anthropic/v1/messages",)),
            ModelServiceEntry("system.ai.glm-5-3", chat),
            ModelServiceEntry("system.ai.gemini-3-1-pro", chat),
            ModelServiceEntry("system.ai.llama-4-maverick", chat),
            ModelServiceEntry("system.ai.bge-large-en", ("mlflow/v1/embeddings",)),
            ModelServiceEntry("system.ai.grok-embed-1", ("mlflow/v1/embeddings",)),
        ]
        assert grok.catalog_from_listing(entries) == [
            "system.ai.grok-4-7",
            "system.ai.grok-4-6",
            "system.ai.claude-opus-5-5",
            "system.ai.claude-opus-4-8",
            "system.ai.claude-haiku-4-5",
            "system.ai.gpt-5-4-nano",
            "system.ai.glm-5-3",
        ]


class TestClassifyProviderTarget:
    @pytest.mark.parametrize("provider_type", ["anthropic", "amazon_bedrock", "bedrock_mantle"])
    def test_anthropic_api_types_serve_claude_on_messages(self, provider_type):
        model, reason = grok.classify_provider_target(
            "us.anthropic.claude-sonnet-4-6", provider_type
        )
        assert reason is None
        assert model == grok.GrokModel(
            "us.anthropic.claude-sonnet-4-6", grok.WIRE_MESSAGES, "/ai-gateway/anthropic/v1"
        )

    def test_anthropic_api_types_reject_other_models(self):
        model, reason = grok.classify_provider_target("amazon.titan-text", "amazon_bedrock")
        assert model is None
        assert "only the Claude models" in reason

    @pytest.mark.parametrize(
        ("target", "wire"),
        [
            ("gpt-5.6", grok.WIRE_RESPONSES),
            ("o3", grok.WIRE_RESPONSES),
            ("gpt-4o", grok.WIRE_CHAT_COMPLETIONS),
            ("mistral-large", grok.WIRE_CHAT_COMPLETIONS),
        ],
    )
    def test_openai_types_use_native_openai_route(self, target, wire):
        model, reason = grok.classify_provider_target(target, "openai")
        assert reason is None
        assert model is not None
        assert model.wire == wire
        assert model.route == "/ai-gateway/openai/v1"

    def test_foundry_claude_uses_anthropic_route(self):
        model, _ = grok.classify_provider_target("claude-opus-4-8", "microsoft_foundry")
        assert model is not None
        assert model.wire == grok.WIRE_MESSAGES

    def test_rejects_gemini_and_non_chat_targets(self):
        assert grok.classify_provider_target("gemini-3-pro", "openai")[0] is None
        model, reason = grok.classify_provider_target(
            "text-embedding-3", "openai", ["openai/v1/embeddings"]
        )
        assert model is None
        assert "isn't a chat model" in reason


class TestModelSelection:
    catalog = grok.databricks_catalog(STATE)

    @pytest.mark.parametrize(
        "requested",
        ["system.ai.glm-5-3", "glm-5-3", "GLM-5-3", " glm-5-3 ", "databricks-glm-5-3"],
    )
    def test_resolves_ids_and_short_names(self, requested):
        catalog = [*self.catalog, grok.GrokModel("databricks-glm-5-3", "x", "/r")]
        expected = (
            "databricks-glm-5-3" if requested == "databricks-glm-5-3" else "system.ai.glm-5-3"
        )
        assert grok.resolve_model(requested, catalog).id == expected

    def test_unknown_model_lists_available(self):
        with pytest.raises(RuntimeError, match="isn't available in ug grok") as exc:
            grok.resolve_model("nope", self.catalog)
        assert "system.ai.claude-opus-5-5" in str(exc.value)

    def test_gemini_model_explains_why(self):
        with pytest.raises(RuntimeError, match="tool schemas"):
            grok.resolve_model("gemini-3-1-pro", self.catalog)

    def test_default_prefers_newest_grok_then_opus_then_sonnet_then_haiku(self):
        assert grok.launch_model(self.catalog).id == "system.ai.grok-4-7"
        no_grok = [m for m in self.catalog if "grok" not in m.id]
        assert grok.launch_model(no_grok).id == "system.ai.claude-opus-5-5"
        no_opus = [m for m in no_grok if "opus" not in m.id]
        assert grok.launch_model(no_opus).id == "system.ai.claude-sonnet-5-5"
        gpt_only = [m for m in no_grok if "claude" not in m.id]
        assert grok.launch_model(gpt_only).id == "system.ai.gpt-5-5"

    def test_default_model_from_state(self):
        assert grok.default_model(STATE) == "system.ai.grok-4-7"
        assert grok.default_model(BUCKETS) == "system.ai.claude-opus-5-5"
        assert grok.default_model({}) is None


class TestRenderConfig:
    def test_model_tables_name_session_placeholders_not_credentials(self):
        catalog = grok.databricks_catalog(STATE)
        doc = grok.render_config(tomlkit.document(), catalog, catalog[0])
        table = doc["model"]["system.ai.claude-opus-5-5"]
        assert table["base_url"] == "${UG_GROK_PROXY_URL}/ai-gateway/anthropic/v1"
        assert table["env_key"] == "UG_GROK_SESSION_SECRET"
        assert table["api_backend"] == "messages"
        assert "api_key" not in table
        assert doc["model"]["system.ai.glm-5-3"]["context_window"] == 200000
        # Older Claude models reject the 128K output Grok asks for by default.
        assert table["max_completion_tokens"] == 32000
        assert "max_completion_tokens" not in doc["model"]["system.ai.gpt-5-5"]

    def test_models_section_pins_default_summary_and_allowlist(self):
        catalog = grok.databricks_catalog(STATE)
        start = grok.resolve_model("claude-sonnet-5-5", catalog)
        doc = grok.render_config(tomlkit.document(), catalog, start)
        assert doc["models"]["default"] == "system.ai.claude-sonnet-5-5"
        # Grok would otherwise title sessions with an xAI model the gateway doesn't serve.
        assert doc["models"]["session_summary"] == "system.ai.claude-haiku-5-5"
        assert doc["models"]["image_description"] == "system.ai.claude-sonnet-5-5"
        assert list(doc["models"]["allowed_models"]) == _ids(catalog)

    def test_summary_falls_back_to_default_without_haiku(self):
        catalog = [grok.GrokModel("system.ai.gpt-5-5", grok.WIRE_RESPONSES, "/r")]
        doc = grok.render_config(tomlkit.document(), catalog, catalog[0])
        assert doc["models"]["session_summary"] == "system.ai.gpt-5-5"

    def test_replaces_ug_models_and_keeps_user_settings(self):
        doc = tomlkit.parse(
            """
[ui]
theme = "auto"

[models]
web_search = "mine"

[model.mine]
model = "my-model"
env_key = "MY_KEY"

[model."system.ai.stale"]
model = "system.ai.stale"
env_key = "UG_GROK_SESSION_SECRET"

[mcp_servers.tool]
command = "x"
"""
        )
        catalog = grok.databricks_catalog(STATE)
        doc = grok.render_config(doc, catalog, catalog[0])
        assert doc["ui"]["theme"] == "auto"
        assert doc["models"]["web_search"] == "mine"
        assert doc["model"]["mine"]["env_key"] == "MY_KEY"
        assert "system.ai.stale" not in doc["model"]
        assert doc["mcp_servers"]["tool"]["command"] == "x"


class TestWriteToolConfig:
    def test_writes_private_config_and_marks_managed(self):
        state = grok.write_tool_config(dict(STATE), "kimi-k3")
        doc = _read_config()
        assert doc["models"]["default"] == "system.ai.kimi-k3"
        assert "system.ai.gpt-5-5" in doc["model"]
        assert state["managed_configs"]["grok"] == {"keys": grok.MANAGED_KEYS}

    def test_backs_up_a_config_ug_did_not_write(self):
        grok.GROK_CONFIG_PATH.parent.mkdir(parents=True)
        grok.GROK_CONFIG_PATH.write_text('[ui]\ntheme = "auto"\n', encoding="utf-8")
        grok.write_tool_config(dict(STATE), None)
        assert grok.GROK_BACKUP_PATH.read_text(encoding="utf-8") == '[ui]\ntheme = "auto"\n'
        assert _read_config()["ui"]["theme"] == "auto"

    def test_no_models_is_an_actionable_error(self):
        with pytest.raises(RuntimeError, match="ug configure --agents grok"):
            grok.write_tool_config({"workspace": WS}, None)

    def test_unknown_model_fails_before_writing(self):
        with pytest.raises(RuntimeError, match="isn't available"):
            grok.write_tool_config(dict(STATE), "nope")
        assert not grok.GROK_CONFIG_PATH.exists()


def _service(**overrides):
    service = {
        "name": "cat.sch.svc",
        "provider_type": "openai",
        "targets": ["gpt-5.6", "gpt-4o", "text-embedding-3"],
        "allow_all_targets": False,
        "target_api_types": {"text-embedding-3": ["openai/v1/embeddings"]},
    }
    service.update(overrides)
    return service


class TestProviderServiceCatalog:
    @staticmethod
    def _catalog(service, requested=None, listed=([], None)):
        with (
            patch.object(grok, "launch_token", return_value="tok"),
            patch.object(grok, "resolve_provider_service", return_value=(service, None)) as resolve,
            patch.object(grok, "list_provider_service_models", return_value=listed) as listing,
            patch.object(grok, "print_warning") as warn,
        ):
            catalog = grok.provider_service_catalog(dict(STATE), "cat.sch.svc", requested)
        return catalog, resolve, listing, warn

    def test_registers_usable_targets_and_warns_about_the_rest(self):
        catalog, resolve, listing, warn = self._catalog(_service())
        assert _ids(catalog) == ["gpt-5.6", "gpt-4o"]
        assert resolve.call_args.args[0] == "grok"
        listing.assert_not_called()
        assert "text-embedding-3" in warn.call_args.args[0]

    def test_allow_all_adds_listing_and_requested_model(self):
        catalog, _, listing, _ = self._catalog(
            _service(targets=[], allow_all_targets=True),
            requested="gpt-5.5",
            listed=(["gpt-5.6"], None),
        )
        assert listing.called
        assert _ids(catalog) == ["gpt-5.5", "gpt-5.6"]

    def test_allow_all_listing_failure_without_model_is_actionable(self):
        with pytest.raises(RuntimeError, match="ug grok --model"):
            self._catalog(_service(targets=[], allow_all_targets=True), listed=([], "boom"))

    def test_service_errors_surface(self):
        with (
            patch.object(grok, "launch_token", return_value="tok"),
            patch.object(grok, "resolve_provider_service", return_value=(None, "not found")),
            pytest.raises(RuntimeError, match="not found"),
        ):
            grok.provider_service_catalog(dict(STATE), "cat.sch.svc", None)

    def test_no_usable_target_is_an_error(self):
        with pytest.raises(RuntimeError, match="has no models ug grok can use"):
            self._catalog(_service(provider_type="anthropic", targets=["titan"]))

    def test_write_tool_config_under_provider_registers_service_models(self):
        service = _service(provider_type="anthropic", targets=["claude-sonnet-4-6"])
        with (
            patch.object(grok, "launch_token", return_value="tok"),
            patch.object(grok, "resolve_provider_service", return_value=(service, None)),
        ):
            grok.write_tool_config(dict(STATE), None, provider="cat.sch.svc")
        doc = _read_config()
        assert list(doc["models"]["allowed_models"]) == ["claude-sonnet-4-6"]
        assert doc["models"]["default"] == "claude-sonnet-4-6"


class TestMcpConfig:
    def test_add_and_remove_preserve_models(self):
        grok.write_tool_config(dict(STATE), None)
        argv = ["/bin/ug", "mcp-proxy", "--url", "https://x/mcp"]
        assert grok.write_mcp_server_config("srv", argv) is False
        assert grok.write_mcp_server_config("srv", argv) is True
        doc = _read_config()
        assert doc["mcp_servers"]["srv"]["command"] == "/bin/ug"
        assert list(doc["mcp_servers"]["srv"]["args"]) == argv[1:]
        assert "system.ai.gpt-5-5" in doc["model"]
        assert grok.remove_mcp_server_config("srv") is True
        assert grok.remove_mcp_server_config("srv") is False
        assert "srv" not in _read_config().get("mcp_servers", {})

    def test_batched_writes_report_only_real_removals(self):
        removed = grok.write_user_mcp_servers(
            {"a": grok.build_mcp_server_entry(["ug", "x"])}, {"missing"}
        )
        assert removed == set()
        assert grok.write_user_mcp_servers({}, {"a", "missing"}) == {"a"}


class TestRuntime:
    def test_env_points_at_session_proxy_and_drops_off_gateway_settings(self, monkeypatch):
        for key in (
            "XAI_API_KEY",
            "GROK_AUTH_PROVIDER_COMMAND",
            "GROK_DEFAULT_MODEL",
            "GROK_MODELS_BASE_URL",
            "GROK_MODELS_LIST_URL",
        ):
            monkeypatch.setenv(key, "x")
        monkeypatch.setenv("KEEP_ME", "1")
        env = grok.build_runtime_env("http://127.0.0.1:1234", "secret")
        assert env["GROK_HOME"] == str(grok.GROK_HOME_DIR)
        assert env["UG_GROK_PROXY_URL"] == "http://127.0.0.1:1234"
        assert env["UG_GROK_SESSION_SECRET"] == "secret"
        assert env["KEEP_ME"] == "1"
        for key in (
            "XAI_API_KEY",
            "GROK_AUTH_PROVIDER_COMMAND",
            "GROK_DEFAULT_MODEL",
            "GROK_MODELS_BASE_URL",
            "GROK_MODELS_LIST_URL",
        ):
            assert key not in env

    def test_rewrites_model_args_before_separator(self):
        catalog = grok.databricks_catalog(STATE)
        args = ["-m", "glm-5-3", "--model=gpt-5-5", "-p", "hi", "--", "-m", "raw"]
        assert grok.rewrite_model_args(args, catalog) == [
            "-m",
            "system.ai.glm-5-3",
            "--model=system.ai.gpt-5-5",
            "-p",
            "hi",
            "--",
            "-m",
            "raw",
        ]

    def test_rewrite_rejects_unknown_model(self):
        with pytest.raises(RuntimeError, match="isn't available"):
            grok.rewrite_model_args(["--model", "nope"], grok.databricks_catalog(STATE))

    def test_proxy_headers(self, monkeypatch):
        monkeypatch.setattr(grok, "agent_version", lambda _binary: "1.0.67")
        headers = grok.proxy_headers("cat.sch.svc")
        assert "grok/1.0.67" in headers["User-Agent"]
        assert headers[MODEL_PROVIDER_SERVICE_HEADER] == "cat.sch.svc"
        assert MODEL_PROVIDER_SERVICE_HEADER not in grok.proxy_headers(None)


class TestLaunch:
    @staticmethod
    def _launch(state, tool_args, options=None):
        server = MagicMock()
        server.server_address = ("127.0.0.1", 4321)
        cache, client = MagicMock(), MagicMock()
        proc = MagicMock()
        proc.wait.return_value = 7
        with (
            patch(
                "ucode.gateway_proxy.start_gateway_auth_proxy",
                return_value=(server, cache, client),
            ) as start,
            patch.object(grok.subprocess_cross_os, "popen", return_value=proc) as popen,
            patch.object(grok, "agent_version", return_value="1.0.67"),
            pytest.raises(SystemExit) as exit_info,
        ):
            grok.launch(state, tool_args, options=options or LaunchOptions())
        return start, popen, exit_info.value.code, (server, cache, client)

    def test_runs_grok_behind_session_proxy(self):
        state = grok.write_tool_config(dict(STATE), None)
        start, popen, code, (server, cache, client) = self._launch(state, ["-m", "glm-5-3"])
        assert code == 7
        kwargs = start.call_args.kwargs
        argv = popen.call_args.args[0]
        env = popen.call_args.kwargs["env"]
        assert argv == ["grok", "-m", "system.ai.glm-5-3"]
        assert env["UG_GROK_PROXY_URL"] == "http://127.0.0.1:4321"
        assert env["UG_GROK_SESSION_SECRET"] == kwargs["session_secret"]
        assert kwargs["dropped_tool_parameter_keys"] == ("$schema",)
        assert kwargs["dropped_message_fields"] == ("model_id",)
        assert MODEL_PROVIDER_SERVICE_HEADER not in kwargs["added_headers"]
        server.shutdown.assert_called_once()
        cache.stop.assert_called_once()
        client.close.assert_called_once()

    def test_launch_provider_adds_routing_header(self):
        state = grok.write_tool_config(dict(STATE), None)
        state[grok.LAUNCH_PROVIDER_SERVICE_KEY] = "cat.sch.svc"
        start, _, _, _ = self._launch(state, [])
        assert start.call_args.kwargs["added_headers"][MODEL_PROVIDER_SERVICE_HEADER] == (
            "cat.sch.svc"
        )
        assert grok.LAUNCH_PROVIDER_SERVICE_KEY not in state

    def test_writes_config_when_none_exists(self):
        _, popen, _, _ = self._launch(dict(STATE), [])
        assert popen.called
        assert _read_config()["models"]["default"] == "system.ai.grok-4-7"

    def test_overlapping_launch_starts_grok_on_its_own_models(self):
        # A configures, then another launch replaces the shared models before A starts Grok.
        state = grok.write_tool_config(dict(STATE), None)
        gpt = grok.GrokModel("gpt-5.6", grok.WIRE_RESPONSES, "/ai-gateway/openai/v1")
        write_toml_file(
            grok.GROK_CONFIG_PATH,
            grok.render_config(read_toml_safe(grok.GROK_CONFIG_PATH), [gpt], gpt),
        )
        seen = {}

        def start_grok(argv, env):
            seen["config"] = _read_config()
            return self._exited()

        with self._patched(popen=start_grok):
            grok.launch(state, ["-m", "glm-5-3"], options=LaunchOptions())

        assert seen["config"]["models"]["default"] == "system.ai.grok-4-7"
        assert seen["config"]["models"]["allowed_models"] == STATE["grok_catalog"]
        assert "gpt-5.6" not in seen["config"]["model"]

    def test_holds_the_config_lock_until_grok_opens_a_session(self):
        state = grok.write_tool_config(dict(STATE), None)
        sessions = grok.GROK_CONFIG_PATH.with_name("active_sessions.json")
        # A session another Grok left open doesn't count; the one this launch opens does.
        sessions.write_text(json.dumps([{"session_id": "earlier", "pid": 1}]), encoding="utf-8")
        proc = MagicMock()
        proc.poll.return_value = None
        proc.wait.return_value = 0
        polls = []

        def poll():
            # Another writer is locked out until Grok lists this process.
            polls.append(self._config_locked())
            if len(polls) == 3:
                opened = [{"session_id": "earlier", "pid": 1}, {"session_id": "new", "pid": 2}]
                sessions.write_text(json.dumps(opened), encoding="utf-8")
            return None

        proc.poll.side_effect = poll
        with self._patched(popen=lambda argv, env: proc):
            grok.launch(state, [], options=LaunchOptions())

        assert len(polls) == 3 and all(polls)
        assert not self._config_locked()

    def test_startup_wait_is_bounded(self, monkeypatch):
        monkeypatch.setattr(grok, "_STARTUP_TIMEOUT_SECONDS", 0.2)
        state = grok.write_tool_config(dict(STATE), None)
        proc = MagicMock()
        proc.poll.return_value = None
        proc.wait.return_value = 3
        with self._patched(popen=lambda argv, env: proc), pytest.raises(SystemExit) as exit_info:
            grok.launch(state, [], options=LaunchOptions())
        assert exit_info.value.code == 3
        assert not self._config_locked()

    @staticmethod
    def _exited():
        proc = MagicMock()
        proc.poll.return_value = 0
        proc.wait.return_value = 0
        return proc

    @staticmethod
    def _config_locked() -> bool:
        lock_path = grok.GROK_CONFIG_PATH.with_name(".ug-config.lock")
        with lock_path.open("a+b") as lock_file:
            try:
                acquire_exclusive_file_lock(lock_file, timeout=0)
            except (TimeoutError, OSError):
                return True
            release_file_lock(lock_file)
            return False

    @staticmethod
    @contextlib.contextmanager
    def _patched(*, popen):
        server = MagicMock()
        server.server_address = ("127.0.0.1", 4321)
        with (
            patch(
                "ucode.gateway_proxy.start_gateway_auth_proxy",
                return_value=(server, MagicMock(), MagicMock()),
            ),
            patch.object(grok.subprocess_cross_os, "popen", side_effect=popen),
            patch.object(grok, "agent_version", return_value="1.0.67"),
            contextlib.suppress(SystemExit),
        ):
            yield

    def test_bad_model_fails_before_starting_proxy(self):
        state = grok.write_tool_config(dict(STATE), None)
        with (
            patch("ucode.gateway_proxy.start_gateway_auth_proxy") as start,
            pytest.raises(RuntimeError, match="isn't available"),
        ):
            grok.launch(state, ["-m", "nope"], options=LaunchOptions())
        start.assert_not_called()


class TestDispatch:
    @pytest.mark.parametrize("name", ["grok", "Grok", "grok-build"])
    def test_aliases(self, name):
        from ucode.agents import normalize_tool

        assert normalize_tool(name) == "grok"

    def test_available_when_a_usable_model_was_discovered(self):
        from ucode.agents import check_gateway_endpoint

        assert check_gateway_endpoint(STATE, "grok")
        assert not check_gateway_endpoint({"gemini_models": ["system.ai.gemini-3-1-pro"]}, "grok")

    def test_configure_tool_writes_config(self):
        from ucode.agents import configure_tool

        state = configure_tool("grok", dict(STATE), "glm-5-3")
        assert state["managed_configs"]["grok"]
        assert _read_config()["models"]["default"] == "system.ai.glm-5-3"

    def test_configure_tool_needs_a_model_or_provider(self):
        from ucode.agents import configure_tool

        with pytest.raises(RuntimeError, match="model must be selected"):
            configure_tool("grok", dict(STATE), None)

    def test_provider_types(self):
        from ucode.databricks import service_usable_for_tool, tool_supports_provider_type

        for provider_type in ("anthropic", "amazon_bedrock", "openai", "microsoft_foundry"):
            assert tool_supports_provider_type("grok", provider_type)
        assert not tool_supports_provider_type("grok", "gemini_enterprise")
        bedrock = {"name": "s", "provider_type": "amazon_bedrock", "targets": ["amazon.titan"]}
        assert not service_usable_for_tool("grok", bedrock)
        relayed = {"name": "s", "provider_type": "anthropic", "targets": [], "relayed": True}
        assert not service_usable_for_tool("grok", relayed)
