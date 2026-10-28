"""Tests for agents/antigravity.py."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock

import httpx
import pytest

from ucode import gateway_proxy
from ucode.agents import antigravity
from ucode.agents.args import LaunchOptions
from ucode.constants import MODEL_PROVIDER_SERVICE_HEADER

WS = "https://example.databricks.com"

GEMINI_MODELS = [
    "system.ai.gemini-4-argon",
    "system.ai.gemini-3-8-flash",
    "system.ai.gemini-3-5-flash-lite",
    "system.ai.gemini-3-1-flash-image",
    "system.ai.gemini-3-1-pro",
    "system.ai.gemini-3-flash",
    "system.ai.gemini-3-pro-image",
]


def _state(**extra) -> dict:
    return {"workspace": WS, "profile": "p", "gemini_models": list(GEMINI_MODELS), **extra}


class TestSpec:
    def test_binary_and_display(self):
        assert antigravity.SPEC["binary"] == "agy"
        assert antigravity.SPEC["display"] == "Antigravity CLI"

    def test_requires_gateway_mode_version(self, monkeypatch):
        monkeypatch.setattr(antigravity, "agent_version", lambda _binary: "1.2.14")

        message = antigravity.minimum_version_error()

        assert message is not None
        assert "1.2.15 or newer" in message
        assert "agy update" in message

    def test_version_gate_checks_the_installed_binary_off_path(self, monkeypatch):
        seen = []
        monkeypatch.setattr(antigravity, "resolve_binary", lambda: "/home/u/.local/bin/agy")
        monkeypatch.setattr(
            antigravity, "agent_version", lambda binary: seen.append(binary) or "1.1.18"
        )

        assert "too old" in (antigravity.minimum_version_error() or "")
        assert seen == ["/home/u/.local/bin/agy"]

    @pytest.mark.parametrize("version", ["1.2.15", "1.4.9", "unknown"])
    def test_supported_or_unknown_version_passes(self, monkeypatch, version):
        monkeypatch.setattr(antigravity, "agent_version", lambda _binary: version)

        assert antigravity.minimum_version_error() is None


class TestModelNames:
    @pytest.mark.parametrize(
        ("databricks", "expected"),
        [
            ("system.ai.gemini-3-8-flash", "gemini-3.8-flash"),
            ("system.ai.gemini-4-argon", "gemini-4-argon"),
            ("system.ai.gemini-3-5-flash-lite", "gemini-3.5-flash-lite"),
            ("system.ai.gemini-3-1-pro", "gemini-3.1-pro-preview"),
            ("system.ai.gemini-3-1-flash-lite", "gemini-3.1-flash-lite-preview"),
            ("system.ai.gemini-3-flash", "gemini-3-flash-preview"),
            ("databricks-gemini-3-7-flash", "gemini-3.7-flash"),
            ("main.models.gemini-3-6-flash", "gemini-3.6-flash"),
            # A provider service's targets already use Google's names.
            ("gemini-3.1-pro-preview", "gemini-3.1-pro-preview"),
        ],
    )
    def test_maps_databricks_ids_to_google_names(self, databricks, expected):
        assert antigravity.agy_model_name(databricks) == expected

    def test_catalog_drops_image_models_and_keeps_newest_first(self):
        catalog = antigravity.model_catalog(_state())

        assert list(catalog) == [
            "gemini-4-argon",
            "gemini-3.8-flash",
            "gemini-3.5-flash-lite",
            "gemini-3.1-pro-preview",
            "gemini-3-flash-preview",
        ]
        assert catalog["gemini-3.8-flash"] == "system.ai.gemini-3-8-flash"

    def test_catalog_keeps_first_endpoint_for_a_shared_name(self):
        state = _state(gemini_models=["system.ai.gemini-3-8-flash", "databricks-gemini-3-8-flash"])

        assert antigravity.model_catalog(state) == {
            "gemini-3.8-flash": "system.ai.gemini-3-8-flash"
        }

    def test_catalog_under_provider_is_the_pinned_target(self):
        state = _state(**{antigravity.PROVIDER_MODEL_STATE_KEY: "gemini-3.1-pro-preview"})

        assert antigravity.model_catalog(state, "cat.sch.svc") == {
            "gemini-3.1-pro-preview": "gemini-3.1-pro-preview"
        }

    def test_catalog_empty_without_gemini_models(self):
        assert antigravity.model_catalog({"workspace": WS}) == {}

    def test_default_model_is_newest_databricks_model(self):
        assert antigravity.default_model(_state()) == "system.ai.gemini-4-argon"

    @pytest.mark.parametrize(
        ("requested", "expected"),
        [
            ("system.ai.gemini-3-8-flash", "gemini-3.8-flash"),
            ("gemini-3.8-flash", "gemini-3.8-flash"),
            ("gemini-3.8-flash-high", "gemini-3.8-flash-high"),
        ],
    )
    def test_launch_model_accepts_databricks_id_name_or_variant(self, requested, expected):
        catalog = antigravity.model_catalog(_state())

        assert antigravity._resolve_launch_model(requested, catalog) == expected

    def test_unknown_launch_model_is_actionable(self):
        catalog = antigravity.model_catalog(_state())

        with pytest.raises(RuntimeError, match="not configured.*ug configure --agent antigravity"):
            antigravity._resolve_launch_model("system.ai.claude-sonnet-5-5", catalog)


class TestRequestRewrites:
    def test_rewrites_model_in_gemini_path(self):
        catalog = {"gemini-3.8-flash": "system.ai.gemini-3-8-flash"}

        path = antigravity.rewrite_model_path(
            "/ai-gateway/gemini/v1beta/models/gemini-3.8-flash:streamGenerateContent?alt=sse",
            catalog,
        )

        assert path == (
            "/ai-gateway/gemini/v1beta/models/system.ai.gemini-3-8-flash"
            ":streamGenerateContent?alt=sse"
        )

    def test_leaves_unknown_model_and_other_paths_alone(self):
        catalog = {"gemini-3.8-flash": "system.ai.gemini-3-8-flash"}
        unknown = "/ai-gateway/gemini/v1beta/models/other:generateContent"

        assert antigravity.rewrite_model_path(unknown, catalog) == unknown
        assert antigravity.rewrite_model_path("/ai-gateway/gemini/v1beta/x", catalog) == (
            "/ai-gateway/gemini/v1beta/x"
        )

    def test_drops_include_thoughts_when_thinking_is_off(self):
        body = json.dumps(
            {
                "contents": [],
                "generationConfig": {
                    "maxOutputTokens": 10,
                    "thinkingConfig": {"includeThoughts": True, "thinkingBudget": 0},
                },
            }
        ).encode()

        rewritten = json.loads(
            antigravity.rewrite_request_body("/ai-gateway/gemini/v1beta/m", body)
        )

        assert rewritten["generationConfig"] == {
            "maxOutputTokens": 10,
            "thinkingConfig": {"thinkingBudget": 0},
        }

    @pytest.mark.parametrize(
        "body",
        [
            {
                "generationConfig": {
                    "thinkingConfig": {"includeThoughts": True, "thinkingBudget": -1}
                }
            },
            {"generationConfig": {"thinkingConfig": {"thinkingBudget": 0}}},
            {"contents": []},
        ],
    )
    def test_keeps_thinking_requests_byte_identical(self, body):
        raw = json.dumps(body).encode()

        assert antigravity.rewrite_request_body("/ai-gateway/gemini/v1beta/m", raw) is raw

    def test_keeps_non_json_bodies(self):
        assert antigravity.rewrite_request_body("/ai-gateway/gemini/x", b"not json") == b"not json"


class TestProxyHeaders:
    def test_user_agent_identifies_antigravity(self, monkeypatch):
        monkeypatch.setattr(antigravity, "agent_version", lambda _binary: "1.4.9")

        headers = antigravity.proxy_headers(None)

        assert "antigravity/1.4.9" in headers["User-Agent"]
        assert MODEL_PROVIDER_SERVICE_HEADER not in headers

    def test_provider_routes_by_header(self):
        headers = antigravity.proxy_headers("cat.sch.svc")

        assert headers[MODEL_PROVIDER_SERVICE_HEADER] == "cat.sch.svc"


class TestRuntimeEnv:
    def test_sets_gateway_mode_and_drops_org_gateway_overrides(self, monkeypatch):
        monkeypatch.setenv("AGY_LLM_GATEWAY_CA_CERT", "/etc/ca.pem")
        monkeypatch.setenv("AGY_LLM_GATEWAY_HEADERS", "X-Team: a")

        env = antigravity.build_runtime_env(
            "http://127.0.0.1:1/ai-gateway/gemini", "secret", ["gemini-3.8-flash", "gemini-4-argon"]
        )

        assert env["AGY_LLM_GATEWAY_URL"] == "http://127.0.0.1:1/ai-gateway/gemini"
        assert env["AGY_LLM_GATEWAY_API_KEY"] == "secret"
        assert env["AGY_LLM_GATEWAY_WIRE_PROTOCOL"] == "genai"
        assert env["AGY_LLM_GATEWAY_MODELS"] == "gemini-3.8-flash,gemini-4-argon"
        assert "AGY_LLM_GATEWAY_CA_CERT" not in env
        assert "AGY_LLM_GATEWAY_HEADERS" not in env

    def test_model_arg_is_added_or_replaced(self):
        assert antigravity._with_model_arg(["-p", "hi"], "gemini-3.8-flash-high") == [
            "--model",
            "gemini-3.8-flash-high",
            "-p",
            "hi",
        ]
        assert antigravity._with_model_arg(["--model", "x", "-p", "hi"], "y") == [
            "--model",
            "y",
            "-p",
            "hi",
        ]
        assert antigravity._with_model_arg(["--model=x"], "y") == ["--model=y"]


# `agy models` in gateway mode, captured from agy 1.4.9.
AGY_MODELS_OUTPUT = (
    "gemini-4-argon-high\tGemini 4 Argon (High)\n"
    "gemini-4-argon-low\tGemini 4 Argon (Low)\n"
    "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
    "gemini-3.8-flash-low\tGemini 3.8 Flash (Low)\n"
    "gemini-3.5-flash-lite\tgemini-3.5-flash-lite\n"
    "gemini-3.1-pro-low-thinking\tGemini 3.1 Pro (Low)\n"
)


class TestCatalogName:
    def test_variant_belongs_to_the_longest_matching_name(self):
        catalog = {"gemini-3.5-flash": "a", "gemini-3.5-flash-lite": "b"}

        assert antigravity._catalog_name("gemini-3.5-flash-lite-high", catalog) == (
            "gemini-3.5-flash-lite"
        )
        assert antigravity._catalog_name("gemini-3.5-flash-high", catalog) == "gemini-3.5-flash"


class TestSelectionId:
    def _select(self, monkeypatch, model, stdout=AGY_MODELS_OUTPUT):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            seen["env"] = kwargs.get("env")
            result = MagicMock()
            result.stdout = stdout
            return result

        monkeypatch.setattr(antigravity.subprocess_cross_os, "run", fake_run)
        selection = antigravity._selection_id("/opt/bin/agy", {"AGY_LLM_GATEWAY_URL": "u"}, model)
        assert seen["argv"] == ["/opt/bin/agy", "models"]
        assert seen["env"] == {"AGY_LLM_GATEWAY_URL": "u"}
        return selection

    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            # Only one tier, named without `-preview`: Antigravity rejects the plain name.
            ("gemini-3.1-pro-preview", "gemini-3.1-pro-low-thinking"),
            # Several tiers: the plain name is accepted and picks Antigravity's default tier.
            ("gemini-3.8-flash", "gemini-3.8-flash"),
            ("gemini-3.8-flash-low", "gemini-3.8-flash-low"),
            ("gemini-3.5-flash-lite", "gemini-3.5-flash-lite"),
        ],
    )
    def test_maps_to_agy_selection_ids(self, monkeypatch, model, expected):
        assert self._select(monkeypatch, model) == expected

    def test_another_models_ids_are_not_counted_as_tiers(self, monkeypatch):
        listing = (
            "gemini-3.5-flash-high\tGemini 3.5 Flash (High)\n"
            "gemini-3.5-flash-lite\tgemini-3.5-flash-lite\n"
            "gemini-3.5-flash-lite-low\tGemini 3.5 Flash Lite (Low)\n"
        )

        def fake_run(argv, **kwargs):
            result = MagicMock()
            result.stdout = listing
            return result

        monkeypatch.setattr(antigravity.subprocess_cross_os, "run", fake_run)

        assert (
            antigravity._selection_id(
                "agy", {}, "gemini-3.5-flash", ["gemini-3.5-flash", "gemini-3.5-flash-lite"]
            )
            == "gemini-3.5-flash-high"
        )

    def test_falls_back_to_the_name_when_listing_fails(self, monkeypatch):
        def fail(argv, **kwargs):
            raise OSError("boom")

        monkeypatch.setattr(antigravity.subprocess_cross_os, "run", fail)

        assert antigravity._selection_id("agy", {}, "gemini-3.8-flash") == "gemini-3.8-flash"


class TestWriteToolConfig:
    def test_declares_downloaded_skills_dir_with_absolute_path(self, monkeypatch, tmp_path):
        monkeypatch.setattr(antigravity, "UG_AGENTS_SKILLS_DIR", tmp_path / ".agents" / "skills")

        state = antigravity.write_tool_config(_state(), "system.ai.gemini-4-argon")

        written = json.loads(antigravity.AGY_SKILLS_CONFIG_PATH.read_text())
        assert written == {"entries": [{"path": str(tmp_path / ".agents" / "skills")}]}
        assert state["managed_configs"]["antigravity"] == {"keys": ["entries"]}

    def test_preserves_user_entries_and_adds_ours_once(self, monkeypatch, tmp_path):
        skills_dir = tmp_path / ".agents" / "skills"
        monkeypatch.setattr(antigravity, "UG_AGENTS_SKILLS_DIR", skills_dir)
        antigravity.AGY_SKILLS_CONFIG_PATH.parent.mkdir(parents=True)
        antigravity.AGY_SKILLS_CONFIG_PATH.write_text(
            json.dumps(
                {
                    "inherits": [{"path": "/team.json"}],
                    "entries": [{"path": "/mine"}, "kept-as-is"],
                }
            )
        )

        antigravity.write_tool_config(_state(), None)
        antigravity.write_tool_config(_state(), None)

        written = json.loads(antigravity.AGY_SKILLS_CONFIG_PATH.read_text())
        assert written["inherits"] == [{"path": "/team.json"}]
        assert written["entries"] == [{"path": "/mine"}, "kept-as-is", {"path": str(skills_dir)}]
        assert not antigravity.AGY_SKILLS_CREATED_MARKER.exists()

    def test_revert_removes_only_our_entry_and_keeps_later_user_edits(self, monkeypatch, tmp_path):
        skills_dir = tmp_path / ".agents" / "skills"
        monkeypatch.setattr(antigravity, "UG_AGENTS_SKILLS_DIR", skills_dir)
        antigravity.AGY_SKILLS_CONFIG_PATH.parent.mkdir(parents=True)
        antigravity.AGY_SKILLS_CONFIG_PATH.write_text(json.dumps({"entries": [{"path": "/mine"}]}))
        antigravity.write_tool_config(_state(), None)
        # The user (or the Antigravity app) edits the shared file after configure.
        config = json.loads(antigravity.AGY_SKILLS_CONFIG_PATH.read_text())
        config["entries"].append({"path": "/later"})
        antigravity.AGY_SKILLS_CONFIG_PATH.write_text(json.dumps(config))

        assert antigravity.revert_skills_config() is True

        assert json.loads(antigravity.AGY_SKILLS_CONFIG_PATH.read_text()) == {
            "entries": [{"path": "/mine"}, {"path": "/later"}]
        }
        assert antigravity.revert_skills_config() is False

    def test_revert_deletes_a_file_ug_created_when_nothing_else_was_added(self):
        antigravity.write_tool_config(_state(), None)
        assert antigravity.AGY_SKILLS_CREATED_MARKER.exists()

        assert antigravity.revert_skills_config() is True

        assert not antigravity.AGY_SKILLS_CONFIG_PATH.exists()
        assert not antigravity.AGY_SKILLS_CREATED_MARKER.exists()

    def test_revert_keeps_a_file_ug_created_once_the_user_adds_to_it(self):
        antigravity.write_tool_config(_state(), None)
        config = json.loads(antigravity.AGY_SKILLS_CONFIG_PATH.read_text())
        config["entries"].append({"path": "/later"})
        antigravity.AGY_SKILLS_CONFIG_PATH.write_text(json.dumps(config))

        antigravity.revert_skills_config()

        assert json.loads(antigravity.AGY_SKILLS_CONFIG_PATH.read_text()) == {
            "entries": [{"path": "/later"}]
        }

    def test_provider_persists_routing_and_target(self):
        state = antigravity.write_tool_config(
            _state(), "gemini-3.1-pro-preview", provider="cat.sch.svc"
        )

        assert state["provider_services"]["antigravity"] == "cat.sch.svc"
        assert antigravity.persisted_provider_model(state) == "gemini-3.1-pro-preview"

    def test_databricks_model_is_not_persisted(self):
        state = antigravity.write_tool_config(_state(), "system.ai.gemini-3-8-flash")

        assert antigravity.persisted_provider_model(state) is None


class TestMcpConfig:
    def test_stdio_entry_matches_agy_mcp_add(self):
        assert antigravity.build_mcp_server_entry(["/bin/ug", "mcp-proxy", "--url", "u"]) == {
            "command": "/bin/ug",
            "args": ["mcp-proxy", "--url", "u"],
            "disabled": False,
        }

    def test_write_and_remove_keep_other_servers(self):
        path = antigravity.AGY_MCP_CONFIG_PATH
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"mcpServers": {"mine": {"serverUrl": "https://x"}}}))

        assert antigravity.write_mcp_server_config("ug-sql", ["/bin/ug", "mcp-proxy"]) is False
        assert antigravity.write_mcp_server_config("ug-sql", ["/bin/ug", "mcp-proxy"]) is True
        servers = json.loads(path.read_text())["mcpServers"]
        assert set(servers) == {"mine", "ug-sql"}

        assert antigravity.remove_mcp_server_config("ug-sql") is True
        assert antigravity.remove_mcp_server_config("ug-sql") is False
        assert set(json.loads(path.read_text())["mcpServers"]) == {"mine"}

    def test_batched_writer_reports_removed_names(self):
        antigravity.write_user_mcp_servers({"a": {"command": "x"}, "b": {"command": "y"}}, set())

        removed = antigravity.write_user_mcp_servers({}, {"a", "missing"})

        assert removed == {"a"}


class _RecordingGateway(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def log_message(self, format, *args):
        return

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        type(self).requests.append(
            {"path": self.path, "headers": dict(self.headers), "body": json.loads(body)}
        )
        payload = b'data: {"candidates": []}\n\n'
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def recording_gateway():
    _RecordingGateway.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingGateway)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", _RecordingGateway.requests
    finally:
        server.shutdown()
        server.server_close()


class TestGatewayProxy:
    """The loopback proxy `launch` starts, exercised over real HTTP."""

    def _start(self, upstream: str, catalog: dict[str, str]):
        return gateway_proxy.start_gateway_auth_proxy(
            upstream,
            lambda _force: "dbx-token",
            session_secret="session-secret",
            added_headers=antigravity.proxy_headers("cat.sch.svc"),
            sign_in_hint="sign in",
            allowed_path_prefixes=(antigravity.GEMINI_GATEWAY_PATH,),
            path_rewriter=lambda path: antigravity.rewrite_model_path(path, catalog),
            body_rewriter=antigravity.rewrite_request_body,
        )

    def test_forwards_with_databricks_token_mapped_model_and_fixed_body(self, recording_gateway):
        upstream, requests = recording_gateway
        server, cache, client = self._start(
            upstream, {"gemini-3.8-flash": "system.ai.gemini-3-8-flash"}
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            port = server.server_address[1]
            response = httpx.post(
                f"http://127.0.0.1:{port}/ai-gateway/gemini/v1beta/models/"
                "gemini-3.8-flash:streamGenerateContent?alt=sse",
                headers={"Authorization": "Bearer session-secret"},
                json={
                    "generationConfig": {
                        "thinkingConfig": {"includeThoughts": True, "thinkingBudget": 0}
                    }
                },
            )
        finally:
            cache.stop()
            server.shutdown()
            server.server_close()
            client.close()

        assert response.status_code == 200
        [request] = requests
        assert request["path"] == (
            "/ai-gateway/gemini/v1beta/models/system.ai.gemini-3-8-flash"
            ":streamGenerateContent?alt=sse"
        )
        assert request["headers"]["Authorization"] == "Bearer dbx-token"
        assert request["headers"][MODEL_PROVIDER_SERVICE_HEADER] == "cat.sch.svc"
        assert request["body"] == {"generationConfig": {"thinkingConfig": {"thinkingBudget": 0}}}

    @pytest.mark.parametrize(
        ("path", "auth", "status"),
        [
            ("/ai-gateway/gemini/v1beta/models/m:generateContent", "Bearer wrong", 401),
            ("/ai-gateway/anthropic/v1/messages", "Bearer session-secret", 404),
            ("/api/2.0/serving-endpoints", "Bearer session-secret", 404),
        ],
    )
    def test_rejects_bad_secret_and_non_gemini_paths(self, recording_gateway, path, auth, status):
        upstream, requests = recording_gateway
        server, cache, client = self._start(upstream, {})
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            port = server.server_address[1]
            response = httpx.post(
                f"http://127.0.0.1:{port}{path}", headers={"Authorization": auth}, json={}
            )
        finally:
            cache.stop()
            server.shutdown()
            server.server_close()
            client.close()

        assert response.status_code == status
        assert requests == []


class TestLaunch:
    def _launch(self, monkeypatch, state, tool_args, options=None):
        captured: dict = {}
        options = options or LaunchOptions()

        def fake_proxy(workspace, token_provider, **kwargs):
            captured["proxy"] = {"workspace": workspace, **kwargs}
            server = MagicMock()
            server.server_address = ("127.0.0.1", 4321)
            return server, MagicMock(), MagicMock()

        def fake_popen(argv, env):
            captured["argv"] = argv
            captured["env"] = env
            proc = MagicMock()
            proc.wait.return_value = 0
            return proc

        monkeypatch.setattr(gateway_proxy, "start_gateway_auth_proxy", fake_proxy)
        monkeypatch.setattr(antigravity.subprocess_cross_os, "popen", fake_popen)
        monkeypatch.setattr(antigravity, "resolve_binary", lambda: "/opt/bin/agy")
        monkeypatch.setattr(
            antigravity,
            "_selection_id",
            lambda binary, env, model, catalog: (
                "gemini-3.1-pro-low-thinking" if model == "gemini-3.1-pro-preview" else model
            ),
        )
        with pytest.raises(SystemExit) as exc:
            antigravity.launch(state, tool_args, options=options)
        assert exc.value.code == 0
        return captured

    def test_launches_agy_in_gateway_mode_on_newest_model(self, monkeypatch):
        captured = self._launch(monkeypatch, _state(), ["-p", "hi"])

        # Antigravity starts on the first listed model, so no `--model` is passed.
        assert captured["argv"] == ["/opt/bin/agy", "-p", "hi"]
        env = captured["env"]
        assert env["AGY_LLM_GATEWAY_URL"] == "http://127.0.0.1:4321/ai-gateway/gemini"
        assert env["AGY_LLM_GATEWAY_API_KEY"] == captured["proxy"]["session_secret"]
        assert env["AGY_LLM_GATEWAY_MODELS"].split(",")[0] == "gemini-4-argon"
        assert captured["proxy"]["allowed_path_prefixes"] == ("/ai-gateway/gemini/",)
        assert captured["proxy"]["workspace"] == WS

    def test_ug_model_option_selects_and_leads_the_catalog(self, monkeypatch):
        captured = self._launch(
            monkeypatch,
            _state(),
            [],
            LaunchOptions(user_pinned_model="system.ai.gemini-3-8-flash"),
        )

        assert captured["argv"] == ["/opt/bin/agy", "--model", "gemini-3.8-flash"]
        assert captured["env"]["AGY_LLM_GATEWAY_MODELS"].split(",")[0] == "gemini-3.8-flash"

    def test_forwarded_databricks_model_arg_is_renamed(self, monkeypatch):
        captured = self._launch(monkeypatch, _state(), ["--model", "system.ai.gemini-3-1-pro"])

        assert captured["argv"] == ["/opt/bin/agy", "--model", "gemini-3.1-pro-low-thinking"]
        assert captured["env"]["AGY_LLM_GATEWAY_MODELS"].split(",")[0] == "gemini-3.1-pro-preview"

    def test_variant_selects_its_model_first(self, monkeypatch):
        captured = self._launch(monkeypatch, _state(), ["--model", "gemini-3.8-flash-high"])

        assert captured["argv"] == ["/opt/bin/agy", "--model", "gemini-3.8-flash-high"]
        assert captured["env"]["AGY_LLM_GATEWAY_MODELS"].split(",")[0] == "gemini-3.8-flash"

    def test_subcommands_run_without_model(self, monkeypatch):
        captured = self._launch(monkeypatch, _state(), ["mcp", "list"])

        assert captured["argv"] == ["/opt/bin/agy", "mcp", "list"]

    def test_provider_launch_routes_to_pinned_target(self, monkeypatch):
        state = _state(
            provider_services={"antigravity": "cat.sch.svc"},
            **{antigravity.PROVIDER_MODEL_STATE_KEY: "gemini-3.1-pro-preview"},
        )

        captured = self._launch(monkeypatch, state, [])

        assert captured["argv"] == ["/opt/bin/agy"]
        assert captured["env"]["AGY_LLM_GATEWAY_MODELS"] == "gemini-3.1-pro-preview"
        assert captured["proxy"]["added_headers"][MODEL_PROVIDER_SERVICE_HEADER] == "cat.sch.svc"

    def test_no_gemini_models_is_actionable(self):
        with pytest.raises(RuntimeError, match="No Gemini model is available"):
            antigravity.launch({"workspace": WS}, [], options=LaunchOptions())
