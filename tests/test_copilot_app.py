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
from ucode.agents.copilot import WIRE_ANTHROPIC, WIRE_COMPLETIONS, WIRE_RESPONSES, CopilotModel

BASE = "http://127.0.0.1:43199"
CATALOG = [
    CopilotModel("system.ai.claude-sonnet-4-6", WIRE_ANTHROPIC),
    CopilotModel("system.ai.gpt-5-2", WIRE_RESPONSES),
    CopilotModel("system.ai.glm-5", WIRE_COMPLETIONS),
]


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


def test_registers_the_three_providers_with_their_models(app):
    copilot_app.register(BASE, CATALOG)

    providers = _by_name(app)
    claude, gpt = providers["Databricks Claude"], providers["Databricks GPT"]
    open_models = providers["Databricks Open Models"]
    assert claude["kind"] == "anthropic"
    assert claude["settings"]["baseUrl"] == f"{BASE}/copilot/anthropic"
    assert gpt["kind"] == "custom"
    assert gpt["settings"] == {
        "baseUrl": f"{BASE}/copilot/responses/v1",
        "wireApi": "responses",
        "authKind": "none",
        "headersJson": "{}",
    }
    assert open_models["kind"] == "custom"
    assert open_models["settings"] == {
        "baseUrl": f"{BASE}/copilot/completions/v1",
        "wireApi": "completions",
        "authKind": "none",
        "headersJson": "{}",
    }
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
    copilot_app.register(BASE, CATALOG)
    ids = set(app.providers)
    app.received.clear()

    copilot_app.register("http://127.0.0.1:43200", CATALOG[1:2])

    assert set(app.providers) == ids
    assert all("secret" not in message for message in app.sent("upsert_model_provider"))
    claude = _by_name(app)["Databricks Claude"]
    assert claude["settings"]["baseUrl"] == "http://127.0.0.1:43200/copilot/anthropic"
    # Models the catalog no longer has, and rows ug didn't write, leave ug's providers.
    assert [m["modelId"] for m in app.models.values()] == ["gpt-5-2"]


def test_a_row_the_user_added_to_ugs_provider_is_replaced(app):
    copilot_app.register(BASE, CATALOG)
    claude = _by_name(app)["Databricks Claude"]
    app.models["user-row"] = {"id": "user-row", "providerId": claude["id"], "modelId": "x"}

    copilot_app.register(BASE, CATALOG)

    assert "user-row" not in app.models


def test_two_models_with_one_copilot_id_keep_the_first(app):
    # ug gives Haiku 5+ a Sonnet id so Copilot sends the thinking it requires.
    copilot_app.register(
        BASE,
        [
            CopilotModel("system.ai.claude-sonnet-5-5", WIRE_ANTHROPIC),
            CopilotModel("system.ai.claude-haiku-5-5", WIRE_ANTHROPIC),
        ],
    )

    assert [(m["modelId"], m["wireModel"]) for m in app.models.values()] == [
        ("claude-sonnet-5.5", "system.ai.claude-sonnet-5-5")
    ]


def test_unregister_deletes_only_ugs_providers(app):
    app.providers["mine"] = {"id": "mine", "name": "Databricks Claude", "kind": "anthropic"}
    copilot_app.register(BASE, CATALOG)
    assert len(app.providers) == 4

    assert copilot_app.unregister() is True
    assert len(app.sent("delete_model_provider")) == 3
    assert list(app.providers) == ["mine"]
    assert app.models == {}
    assert copilot_app.unregister() is False


def test_retries_while_the_app_applies_another_change(app):
    app.busy = 2

    copilot_app.register(BASE, CATALOG)

    assert len(app.providers) == 3


def test_a_refusal_stops_the_registration(app):
    app.fail = "upsert_provider_model"

    with pytest.raises(copilot_app.CopilotAppError, match="refused upsert_provider_model"):
        copilot_app.register(BASE, CATALOG)


def test_an_app_that_quits_mid_registration_is_not_running(app):
    app.drop = "upsert_provider_model"

    with pytest.raises(copilot_app.CopilotAppNotRunning):
        copilot_app.register(BASE, CATALOG)


def test_a_listed_model_without_an_id_is_left_alone(app):
    copilot_app.register(BASE, CATALOG)
    claude = _by_name(app)["Databricks Claude"]["id"]
    app.models["odd"] = {"providerId": claude, "modelId": "claude-odd"}

    copilot_app.register(BASE, CATALOG)

    assert "odd" in app.models


def test_an_unknown_app_version_warns_and_still_registers(app, capsys):
    app.version = "9.0.0"

    copilot_app.register(BASE, CATALOG)

    assert "GitHub Copilot app 9.0.0 hasn't been verified with ug" in " ".join(
        capsys.readouterr().out.split()
    )
    assert len(app.providers) == 3


def test_a_closed_app_is_not_running(tmp_path, monkeypatch):
    home = tmp_path / ".copilot"
    monkeypatch.setenv("COPILOT_HOME", str(home))

    with pytest.raises(copilot_app.CopilotAppNotRunning):
        copilot_app.register(BASE, CATALOG)

    # The app leaves its run files behind when it exits.
    (home / "run").mkdir(parents=True)
    (home / "run" / "ws.port").write_text("1\n4242\n")
    (home / "run" / "ws.token").write_text("stale\n4242\n")
    with pytest.raises(copilot_app.CopilotAppNotRunning):
        copilot_app.unregister()


def test_a_rejected_token_is_an_app_error(app):
    (copilot_app.copilot_home() / "run" / "ws.token").write_text("wrong\n4242\n")

    with pytest.raises(copilot_app.CopilotAppError, match="refused ug"):
        copilot_app.register(BASE, CATALOG)
