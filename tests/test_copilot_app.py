"""Registering ug's providers in the GitHub Copilot desktop app, against a fake of its local
WebSocket API (message shapes recorded from app 1.4.2)."""

from __future__ import annotations

import http
import json
import threading
from urllib.parse import parse_qs, urlparse

import pytest
from websockets.sync.server import serve

from ucode import copilot_app
from ucode import copilot_app_plugin as plugin
from ucode.agents.copilot import WIRE_ANTHROPIC, WIRE_COMPLETIONS, WIRE_RESPONSES, CopilotModel
from ucode.constants import MODEL_PROVIDER_SERVICE_HEADER

WORKSPACE = "https://example.cloud.databricks.com"
CATALOG = [
    CopilotModel("system.ai.claude-sonnet-4-6", WIRE_ANTHROPIC),
    CopilotModel("system.ai.gpt-5-2", WIRE_RESPONSES),
    CopilotModel("system.ai.glm-5", WIRE_COMPLETIONS),
]


def _app_providers(catalog=CATALOG, workspace=WORKSPACE, **kwargs):
    return copilot_app.desired_providers(workspace, catalog, **kwargs).app


class FakeApp:
    """The app's WebSocket API: provider and model CRUD, the healthcheck, its token and origin
    checks, and an optional injected error."""

    def __init__(self, home, version="1.4.2"):
        self.version = version
        self.token = "per-launch-token"
        self.providers: dict[str, dict] = {}
        self.models: dict[str, dict] = {}
        self.secrets: dict[str, str] = {}
        self.received: list[dict] = []
        self.handshakes: list[dict] = []
        self.busy = 0
        self.fail: str | None = None
        self.drop: str | None = None
        self.before_reply: dict[str, list[dict]] = {}
        self._server = serve(self._handle, "127.0.0.1", 0, process_request=self._check)
        port = self._server.socket.getsockname()[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        run = home / "run"
        run.mkdir(parents=True)
        (run / "ws.port").write_text(f"{port}\n4242\n")
        (run / "ws.token").write_text(f"{self.token}\n4242\n")

    def _check(self, connection, request):
        self.handshakes.append(dict(request.headers.raw_items()))
        if request.headers.get("Origin"):
            return connection.respond(http.HTTPStatus.FORBIDDEN, "origin")
        if parse_qs(urlparse(request.path).query).get("token") != [self.token]:
            return connection.respond(http.HTTPStatus.UNAUTHORIZED, "token")
        return None

    def _reply(self, message):
        kind = message["type"]
        if self.fail == kind:
            return {"type": "error", "message": "refused", "request_id": message.get("request_id")}
        if kind.startswith("upsert") and self.busy:
            self.busy -= 1
            return {"type": "error", "message": "provider update still in progress"}
        if kind == "get_healthcheck":
            return {"type": "command_result", "data": {"app": {"version": self.version}}}
        if kind == "list_model_providers":
            return {"type": "model_providers_list", "providers": list(self.providers.values())}
        if kind == "upsert_model_provider":
            provider = message["provider"]
            if "secret" in message:
                self.secrets[provider["id"]] = message["secret"]["value"]
            self.providers[provider["id"]] = {
                **provider,
                "hasSecret": provider["id"] in self.secrets,
            }
            return {"type": "model_provider_upserted", "provider": self.providers[provider["id"]]}
        if kind == "delete_model_provider":
            provider_id = message["provider_id"]
            self.providers.pop(provider_id)
            self.secrets.pop(provider_id, None)
            self.models = {k: m for k, m in self.models.items() if m["providerId"] != provider_id}
            return {"type": "model_provider_deleted", "provider_id": provider_id}
        if kind == "list_provider_models":
            return {"type": "provider_models_list", "models": list(self.models.values())}
        if kind == "upsert_provider_model":
            self.models[message["model"]["id"]] = message["model"]
            return {"type": "provider_model_upserted", "model": message["model"]}
        if kind == "delete_provider_model":
            self.models.pop(message["model_id"])
            return {"type": "provider_model_deleted", "model_id": message["model_id"]}
        return {"type": "error", "message": f"unknown variant `{kind}`"}

    def _handle(self, websocket):
        websocket.send(json.dumps({"type": "server_hello", "connection_id": 1}))
        websocket.send(json.dumps({"type": "keep_awake_changed"}))
        for text in websocket:
            message = json.loads(text)
            self.received.append(message)
            if message["type"] == self.drop:
                websocket.close()
                return
            for frame in self.before_reply.pop(message["type"], []):
                websocket.send(json.dumps(frame))
            reply = self._reply(message)
            if "request_id" in message and "request_id" not in reply:
                reply["request_id"] = message["request_id"]
            websocket.send(json.dumps(reply))

    def sent(self, kind):
        return [message for message in self.received if message["type"] == kind]

    def stop(self):
        self._server.shutdown()


@pytest.fixture
def app(tmp_path, monkeypatch):
    home = tmp_path / ".copilot"
    monkeypatch.setenv("COPILOT_HOME", str(home))
    fake = FakeApp(home)
    yield fake
    fake.stop()


def _by_name(app):
    return {provider["name"]: provider for provider in app.providers.values()}


class TestDesiredProviders:
    def test_the_plugin_gets_the_app_rows_ids(self):
        desired = copilot_app.desired_providers(WORKSPACE, CATALOG)

        assert len(desired.app) == len(desired.plugin) == 3
        for app_provider, plugin_provider in zip(desired.app, desired.plugin, strict=True):
            assert plugin_provider.id == app_provider.id
            assert [
                (model.model_id, model.wire_model, model.name) for model in plugin_provider.models
            ] == [
                (row["modelId"], row["wireModel"], row["displayName"])
                for row in app_provider.models
            ]
            assert app_provider.settings["baseUrl"] == WORKSPACE + plugin_provider.base_path

    def test_the_plugin_config_names_the_app_providers(self):
        desired = copilot_app.desired_providers(WORKSPACE, CATALOG)
        config = plugin.extension_config(
            plugin.PluginConfig(workspace=WORKSPACE, ug_path="/opt/ug", providers=desired.plugin)
        )

        app_ids = {(p.id, row["modelId"]) for p in desired.app for row in p.models}
        assert {(m["provider"], m["id"]) for m in config["models"]} == app_ids
        assert [p["name"] for p in config["providers"]] == [p.id for p in desired.app]

    def test_the_three_wires(self):
        claude, gpt, open_models = copilot_app.desired_providers(WORKSPACE + "/", CATALOG).plugin

        assert (claude.wire, claude.base_path) == ("anthropic", "/ai-gateway/anthropic")
        assert (gpt.wire, gpt.base_path) == ("responses", "/ai-gateway/mlflow/v1")
        assert (open_models.wire, open_models.base_path) == ("completions", "/ai-gateway/mlflow/v1")
        app_claude, app_gpt, app_open = _app_providers(workspace=WORKSPACE + "/")
        assert (app_claude.kind, app_claude.settings) == (
            "anthropic",
            {
                "baseUrl": f"{WORKSPACE}/ai-gateway/anthropic",
                "wireApi": "completions",
                "authKind": "api_key",
                "headersJson": "{}",
            },
        )
        assert app_claude.secret == {"kind": "apiKey", "value": copilot_app.PLACEHOLDER_KEY}
        assert (app_gpt.kind, app_gpt.settings, app_gpt.secret) == (
            "custom",
            {
                "baseUrl": f"{WORKSPACE}/ai-gateway/mlflow/v1",
                "wireApi": "responses",
                "authKind": "none",
                "headersJson": "{}",
            },
            None,
        )
        assert (app_open.kind, app_open.settings["wireApi"], app_open.secret) == (
            "custom",
            "completions",
            None,
        )

    def test_a_model_provider_service_uses_the_openai_route_and_its_header(self):
        desired = copilot_app.desired_providers(WORKSPACE, CATALOG, provider_service="my-mps")

        assert [p.base_path for p in desired.plugin] == [
            "/ai-gateway/anthropic",
            "/ai-gateway/openai/v1",
            "/ai-gateway/openai/v1",
        ]
        assert desired.headers == {MODEL_PROVIDER_SERVICE_HEADER: "my-mps"}
        assert [m.name for p in desired.plugin for m in p.models] == [m.id for m in CATALOG]
        assert copilot_app.desired_providers(WORKSPACE, CATALOG).headers == {}

    def test_a_wire_without_models_gets_no_provider(self):
        desired = copilot_app.desired_providers(WORKSPACE, CATALOG[1:2])

        assert [p.name for p in desired.app] == ["Databricks GPT"]
        assert [p.wire for p in desired.plugin] == ["responses"]

    def test_two_models_with_one_copilot_id_keep_the_first(self):
        # ug gives Haiku 5+ a Sonnet id so Copilot sends the thinking it requires.
        (claude,) = copilot_app.desired_providers(
            WORKSPACE,
            [
                CopilotModel("system.ai.claude-sonnet-5-5", WIRE_ANTHROPIC),
                CopilotModel("system.ai.claude-haiku-5-5", WIRE_ANTHROPIC),
            ],
        ).plugin

        assert [(m.model_id, m.wire_model) for m in claude.models] == [
            ("claude-sonnet-5.5", "system.ai.claude-sonnet-5-5")
        ]

    def test_state_providers_use_the_vscode_catalog(self, monkeypatch):
        from ucode.agents import vscode

        calls = []

        def models(state, provider, location):
            calls.append((provider, location))
            return CATALOG

        monkeypatch.setattr(vscode, "copilot_models", models)

        desired = copilot_app.state_providers({"workspace": WORKSPACE}, provider="my-mps")

        assert calls == [("my-mps", None)]
        assert desired == copilot_app.desired_providers(
            WORKSPACE, CATALOG, provider_service="my-mps"
        )


def test_registers_the_three_providers_with_their_models(app):
    copilot_app.register(_app_providers())

    providers = _by_name(app)
    claude, gpt = providers["Databricks Claude"], providers["Databricks GPT"]
    open_models = providers["Databricks Open Models"]
    assert claude["settings"]["baseUrl"] == f"{WORKSPACE}/ai-gateway/anthropic"
    assert gpt["settings"]["baseUrl"] == f"{WORKSPACE}/ai-gateway/mlflow/v1"
    assert open_models["settings"]["wireApi"] == "completions"
    # The app stores the placeholder in its own keychain slot; only the Claude provider needs one.
    assert app.secrets == {claude["id"]: copilot_app.PLACEHOLDER_KEY}
    rows = {m["providerId"]: m for m in app.models.values()}
    assert rows[claude["id"]]["modelId"] == "claude-sonnet-4.6"
    assert rows[claude["id"]]["wireModel"] == "system.ai.claude-sonnet-4-6"
    assert rows[claude["id"]]["displayName"] == "Claude Sonnet 4.6"
    assert rows[gpt["id"]]["modelId"] == "gpt-5-2"
    assert rows[gpt["id"]]["wireModel"] == "system.ai.gpt-5-2"
    assert rows[gpt["id"]]["wireApiOverride"] == "responses"
    assert rows[open_models["id"]]["modelId"] == "glm-5"
    assert rows[open_models["id"]]["wireModel"] == "system.ai.glm-5"
    assert "wireApiOverride" not in rows[open_models["id"]]
    assert len(app.models) == 3
    assert all("Origin" not in headers for headers in app.handshakes)


def test_registering_again_updates_in_place_without_resending_the_key(app):
    copilot_app.register(_app_providers())
    claude_id = _by_name(app)["Databricks Claude"]["id"]
    app.received.clear()

    copilot_app.register(_app_providers(workspace="https://other.cloud.databricks.com"))

    assert _by_name(app)["Databricks Claude"]["id"] == claude_id
    assert all("secret" not in message for message in app.sent("upsert_model_provider"))
    assert _by_name(app)["Databricks Claude"]["settings"]["baseUrl"] == (
        "https://other.cloud.databricks.com/ai-gateway/anthropic"
    )


def test_a_catalog_change_drops_ugs_stale_providers_and_models(app):
    app.providers["mine"] = {"id": "mine", "name": "Mine", "kind": "custom"}
    copilot_app.register(_app_providers())

    copilot_app.register(
        _app_providers([CATALOG[1], CopilotModel("system.ai.gpt-6", WIRE_RESPONSES)])
    )

    assert sorted(app.providers) == sorted(["mine", _by_name(app)["Databricks GPT"]["id"]])
    assert sorted(m["modelId"] for m in app.models.values()) == ["gpt-5-2", "gpt-6"]

    copilot_app.register(_app_providers(CATALOG[2:]))

    assert [m["modelId"] for m in app.models.values()] == ["glm-5"]


def test_a_row_the_user_added_to_ugs_provider_is_replaced(app):
    copilot_app.register(_app_providers())
    claude = _by_name(app)["Databricks Claude"]
    app.models["user-row"] = {"id": "user-row", "providerId": claude["id"], "modelId": "x"}

    copilot_app.register(_app_providers())

    assert "user-row" not in app.models


def test_unregister_deletes_only_ugs_providers(app):
    app.providers["mine"] = {"id": "mine", "name": "Databricks Claude", "kind": "anthropic"}
    copilot_app.register(_app_providers())
    assert len(app.providers) == 4

    assert copilot_app.unregister() is True
    assert len(app.sent("delete_model_provider")) == 3
    assert list(app.providers) == ["mine"]
    assert app.models == {}
    assert copilot_app.unregister() is False


class TestStatus:
    def test_registered_outdated_and_not_registered(self, app):
        providers = _app_providers()
        assert copilot_app.status(providers) == "not_registered"

        copilot_app.register(providers)
        assert copilot_app.status(providers) == "registered"
        assert copilot_app.status(_app_providers(CATALOG[:1])) == "outdated"

        newer = _app_providers([*CATALOG, CopilotModel("system.ai.gpt-6", WIRE_RESPONSES)])
        assert copilot_app.status(newer) == "outdated"

        copilot_app.unregister()
        assert copilot_app.status(providers) == "not_registered"

    def test_a_closed_app_is_not_running(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COPILOT_HOME", str(tmp_path / ".copilot"))

        assert copilot_app.status(_app_providers()) == "not_running"

    def test_an_app_that_quits_mid_check_is_not_running(self, app):
        app.drop = "list_model_providers"

        assert copilot_app.status(_app_providers()) == "not_running"


def test_retries_while_the_app_applies_another_change(app):
    app.busy = 2

    copilot_app.register(_app_providers())

    assert len(app.providers) == 3


def test_a_refusal_stops_the_registration(app):
    app.fail = "upsert_provider_model"

    with pytest.raises(copilot_app.CopilotAppError, match="refused upsert_provider_model"):
        copilot_app.register(_app_providers())


def _healthcheck(version, **fields):
    return {"type": "command_result", "data": {"app": {"version": version}}, **fields}


def test_every_request_carries_a_request_id(app):
    copilot_app.register(_app_providers())

    ids = [message.get("request_id") for message in app.received]
    assert all(ids)
    assert len(set(ids)) == len(ids)


@pytest.mark.parametrize(
    "stray",
    [_healthcheck("0.0.1", request_id="ug-someone-else"), _healthcheck("0.0.1")],
    ids=["another-request-id", "no-request-id"],
)
def test_a_reply_not_for_this_request_is_skipped(app, capsys, stray):
    app.before_reply["get_healthcheck"] = [stray]

    copilot_app.register(_app_providers())

    assert "hasn't been verified" not in capsys.readouterr().out
    assert len(app.providers) == 3


def test_an_error_for_another_request_is_skipped(app):
    app.before_reply["get_healthcheck"] = [
        {"type": "error", "message": "boom", "request_id": "ug-someone-else"}
    ]

    copilot_app.register(_app_providers())

    assert len(app.providers) == 3


def test_an_error_without_a_request_id_answers_the_pending_request(app):
    app.before_reply["get_healthcheck"] = [{"type": "error", "message": "Invalid message format"}]

    with pytest.raises(copilot_app.CopilotAppError, match="Invalid message format"):
        copilot_app.register(_app_providers())


def test_an_app_that_quits_mid_registration_is_not_running(app):
    app.drop = "upsert_provider_model"

    with pytest.raises(copilot_app.CopilotAppNotRunning):
        copilot_app.register(_app_providers())


def test_a_listed_model_without_an_id_is_left_alone(app):
    copilot_app.register(_app_providers())
    claude = _by_name(app)["Databricks Claude"]["id"]
    app.models["odd"] = {"providerId": claude, "modelId": "claude-odd"}

    copilot_app.register(_app_providers())

    assert "odd" in app.models


def test_an_unknown_app_version_warns_and_still_registers(app, capsys):
    app.version = "9.0.0"

    copilot_app.register(_app_providers())

    assert "GitHub Copilot app 9.0.0 hasn't been verified with ug" in " ".join(
        capsys.readouterr().out.split()
    )
    assert len(app.providers) == 3


def test_a_verified_app_version_registers_quietly(app, capsys):
    copilot_app.register(_app_providers())

    assert "hasn't been verified" not in capsys.readouterr().out


def test_a_closed_app_is_not_running(tmp_path, monkeypatch):
    home = tmp_path / ".copilot"
    monkeypatch.setenv("COPILOT_HOME", str(home))

    with pytest.raises(copilot_app.CopilotAppNotRunning):
        copilot_app.register(_app_providers())

    # The app leaves its run files behind when it exits.
    (home / "run").mkdir(parents=True)
    (home / "run" / "ws.port").write_text("1\n4242\n")
    (home / "run" / "ws.token").write_text("stale\n4242\n")
    with pytest.raises(copilot_app.CopilotAppNotRunning):
        copilot_app.unregister()


def test_a_rejected_token_is_an_app_error(app):
    (copilot_app.copilot_home() / "run" / "ws.token").write_text("wrong\n4242\n")

    with pytest.raises(copilot_app.CopilotAppError, match="refused ug"):
        copilot_app.register(_app_providers())
