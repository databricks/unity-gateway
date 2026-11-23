"""Registering ug's providers in the GitHub Copilot desktop app, against a fake of its local
WebSocket API (message shapes recorded from app 1.4.2)."""

from __future__ import annotations

import http
import json
import os
import threading
from urllib.parse import parse_qs, urlparse

import pytest
from websockets.sync.server import serve

from ucode.agents.copilot import WIRE_ANTHROPIC, WIRE_COMPLETIONS, WIRE_RESPONSES, CopilotModel
from ucode.agents.desktop import copilot_app
from ucode.agents.desktop import copilot_app_plugin as plugin
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
        self.paths: list[str] = []
        self.busy = 0
        self.busy_kinds: tuple[str, ...] = ("upsert",)
        self.fail: str | None = None
        self.drop: str | None = None
        self.before_reply: dict[str, list[dict]] = {}
        self.before_hello: list[dict] = []
        self._server = serve(self._handle, "127.0.0.1", 0, process_request=self._check)
        port = self._server.socket.getsockname()[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        run = home / "run"
        run.mkdir(parents=True)
        (run / "ws.port").write_text(f"{port}\n{os.getpid()}\n")
        (run / "ws.token").write_text(f"{self.token}\n{os.getpid()}\n")

    def _check(self, connection, request):
        self.handshakes.append(dict(request.headers.raw_items()))
        self.paths.append(request.path)
        if request.headers.get("Origin"):
            return connection.respond(http.HTTPStatus.FORBIDDEN, "origin")
        if parse_qs(urlparse(request.path).query).get("token") != [self.token]:
            return connection.respond(http.HTTPStatus.UNAUTHORIZED, "token")
        return None

    def _reply(self, message):
        kind = message["type"]
        if self.fail == kind:
            return {"type": "error", "message": "refused", "request_id": message.get("request_id")}
        if kind.startswith(self.busy_kinds) and self.busy:
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
        for frame in self.before_hello:
            websocket.send(json.dumps(frame))
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
                (model.model_id, model.wire_model, model.name, model.limits, model.vision)
                for model in plugin_provider.models
            ] == [
                (
                    row["modelId"],
                    row["wireModel"],
                    row["displayName"],
                    (row["maxPromptTokens"], row["maxOutputTokens"])
                    if "maxPromptTokens" in row
                    else None,
                    row["supportsVision"],
                )
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
        assert [m.name for p in desired.plugin for m in p.models] == [
            m.id + copilot_app.APP_LABEL_SUFFIX for m in CATALOG
        ]

    def test_rows_are_labelled_as_databricks_models(self):
        desired = copilot_app.desired_providers(WORKSPACE, CATALOG)

        names = [row["displayName"] for p in desired.app for row in p.models]
        assert names
        assert all(name.endswith(" (Databricks)") for name in names)
        assert [m.name for p in desired.plugin for m in p.models] == names
        assert copilot_app.desired_providers(WORKSPACE, CATALOG).headers == {}

    def test_only_claude_rows_use_copilot_catalog_ids(self):
        catalog = [
            *CATALOG,
            CopilotModel("system.ai.kimi-k3", WIRE_COMPLETIONS),
            CopilotModel("gpt-5.2", WIRE_RESPONSES),
        ]
        for desired in (
            copilot_app.desired_providers(WORKSPACE, catalog),
            copilot_app.desired_providers(WORKSPACE, catalog, provider_service="my-mps"),
        ):
            plugin_ids = {p.wire: [m.model_id for m in p.models] for p in desired.plugin}
            assert plugin_ids == {
                "anthropic": ["claude-sonnet-4.6"],
                "responses": ["system.ai.gpt-5-2", "databricks.gpt-5.2"],
                "completions": ["system.ai.glm-5", "system.ai.kimi-k3"],
            }
            app_ids = [row["modelId"] for p in desired.app for row in p.models]
            assert app_ids == [model_id for ids in plugin_ids.values() for model_id in ids]

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

    def test_a_claude_service_gets_the_id_of_the_model_behind_it(self, capsys):
        (claude,) = copilot_app.desired_providers(
            WORKSPACE,
            [
                CopilotModel(
                    "main.team.team-coder",
                    WIRE_ANTHROPIC,
                    served_model="system.ai.claude-sonnet-4-6",
                )
            ],
        ).plugin

        assert [(m.model_id, m.wire_model) for m in claude.models] == [
            ("claude-sonnet-4.6", "main.team.team-coder")
        ]
        assert capsys.readouterr().out == ""

    def test_a_claude_route_model_without_a_claude_id_is_skipped_with_a_warning(self, capsys):
        desired = copilot_app.desired_providers(
            WORKSPACE, [CopilotModel("main.team.mystery", WIRE_ANTHROPIC), *CATALOG[1:]]
        )

        assert [p.wire for p in desired.plugin] == ["responses", "completions"]
        assert "Skipping main.team.mystery" in " ".join(capsys.readouterr().out.split())

    def test_a_duplicate_is_skipped_with_a_warning(self, capsys):
        (claude,) = copilot_app.desired_providers(
            WORKSPACE,
            [
                CopilotModel("us.anthropic.claude-sonnet-4-6", WIRE_ANTHROPIC),
                CopilotModel("eu.anthropic.claude-sonnet-4-6", WIRE_ANTHROPIC),
            ],
            provider_service="my-mps",
        ).plugin

        assert [m.wire_model for m in claude.models] == ["us.anthropic.claude-sonnet-4-6"]
        out = " ".join(capsys.readouterr().out.split())
        assert "Skipping eu.anthropic.claude-sonnet-4-6" in out
        assert "us.anthropic.claude-sonnet-4-6" in out

    def test_state_providers_use_the_vscode_catalog(self, monkeypatch):
        from ucode.agents import vscode

        calls = []

        def models(state, provider, location, *, agent):
            calls.append((provider, location, agent))
            return CATALOG

        monkeypatch.setattr(vscode, "copilot_models", models)

        desired = copilot_app.state_providers({"workspace": WORKSPACE}, provider="my-mps")

        assert calls == [("my-mps", None, "the GitHub Copilot app")]
        assert desired == copilot_app.desired_providers(
            WORKSPACE, CATALOG, provider_service="my-mps"
        )


def seed(app, providers):
    """Puts ``providers`` in the fake app the way the plugin writes them."""
    for rows in (provider.rows() for provider in providers):
        app.providers[rows.provider["id"]] = {
            **rows.provider,
            "hasSecret": rows.secret is not None,
        }
        for model in rows.models:
            app.models[model["id"]] = dict(model)


class TestPluginRows:
    def test_the_plugin_writes_the_app_rows(self):
        desired = copilot_app.desired_providers(WORKSPACE, CATALOG)
        rows = desired.plugin_app

        assert [r.provider["id"] for r in rows] == [p.id for p in desired.plugin]
        for provider_rows, provider in zip(rows, desired.app, strict=True):
            assert provider_rows.provider == {
                "id": provider.id,
                "name": provider.name,
                "kind": provider.kind,
                "settings": provider.settings,
            }
            assert provider_rows.secret == provider.secret
            assert [
                (m["id"], m["providerId"], m["modelId"], m["wireModel"])
                for m in provider_rows.models
            ] == [
                (
                    provider.model_row_id(row["modelId"]),
                    provider.id,
                    row["modelId"],
                    row["wireModel"],
                )
                for row in provider.models
            ]
        assert set(copilot_app.OWNED_PROVIDER_IDS) >= {r.provider["id"] for r in rows}
        assert len(copilot_app.OWNED_PROVIDER_IDS) == 3

    def test_the_plugin_rows_and_the_picker_rows_agree(self):
        desired = copilot_app.desired_providers(
            WORKSPACE,
            [
                *CATALOG,
                CopilotModel("system.ai.gpt-6", WIRE_RESPONSES, listed_limits=(922_000, 128_000)),
            ],
        )
        for plugin_provider, provider_rows in zip(desired.plugin, desired.plugin_app, strict=True):
            for model, row in zip(plugin_provider.models, provider_rows.models, strict=True):
                assert (model.model_id, model.wire_model, model.name) == (
                    row["modelId"],
                    row["wireModel"],
                    row["displayName"],
                )
                assert model.vision == row["supportsVision"]
                assert model.limits == (
                    (row["maxPromptTokens"], row["maxOutputTokens"])
                    if "maxPromptTokens" in row
                    else None
                )
        gpt = {m.model_id: m for m in desired.plugin[1].models}
        assert gpt["system.ai.gpt-6"].limits == (794_000, 128_000)

    def test_only_chat_completions_models_turn_off_reasoning_effort(self):
        claude, gpt, open_models = copilot_app.desired_providers(WORKSPACE, CATALOG).plugin

        assert [m.reasoning_effort for m in open_models.models] == [False]
        assert [m.reasoning_effort for m in (*claude.models, *gpt.models)] == [None, None]
        assert [m.vision for m in claude.models] == [True]


def test_unregister_deletes_only_ugs_providers(app):
    app.providers["mine"] = {"id": "mine", "name": "Databricks Claude", "kind": "anthropic"}
    seed(app, _app_providers())
    assert len(app.providers) == 4

    assert copilot_app.unregister() is True
    assert len(app.sent("delete_model_provider")) == 3
    assert list(app.providers) == ["mine"]
    assert app.models == {}
    assert copilot_app.unregister() is False
    assert all("Origin" not in headers for headers in app.handshakes)


class TestStatus:
    def test_registered_outdated_and_not_registered(self, app):
        providers = _app_providers()
        assert copilot_app.status(providers) == "not_registered"

        seed(app, providers)
        assert copilot_app.status(providers) == "registered"
        assert copilot_app.status(_app_providers(CATALOG[:1])) == "outdated"

        newer = _app_providers([*CATALOG, CopilotModel("system.ai.gpt-6", WIRE_RESPONSES)])
        assert copilot_app.status(newer) == "outdated"

        keyed = next(p.id for p in providers if p.secret is not None)
        app.providers[keyed]["hasSecret"] = False
        assert copilot_app.status(providers) == "outdated"
        app.providers[keyed]["hasSecret"] = True

        copilot_app.unregister()
        assert copilot_app.status(providers) == "not_registered"

    def test_another_workspace_or_changed_rows_are_outdated(self, app):
        providers = _app_providers()
        seed(app, providers)

        assert copilot_app.status(
            _app_providers(workspace="https://other.cloud.databricks.com")
        ) == ("outdated")
        renamed = [
            copilot_app.AppProvider(
                p.key,
                p.name,
                p.kind,
                p.settings,
                p.secret,
                tuple({**row, "displayName": "Renamed"} for row in p.models),
            )
            for p in providers
        ]
        assert copilot_app.status(renamed) == "outdated"
        assert copilot_app.status(providers) == "registered"

    def test_a_row_the_user_added_counts_as_outdated(self, app):
        providers = _app_providers()
        seed(app, providers)
        claude = providers[0].id
        app.models["user-row"] = {"id": "user-row", "providerId": claude, "modelId": "x"}

        assert copilot_app.status(providers) == "outdated"

    def test_a_listed_model_without_an_id_is_ignored(self, app):
        providers = _app_providers()
        seed(app, providers)
        app.models["odd"] = {"providerId": providers[0].id, "modelId": "claude-odd"}

        assert copilot_app.status(providers) == "registered"

    def test_a_closed_app_is_not_running(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COPILOT_HOME", str(tmp_path / ".copilot"))

        assert copilot_app.status(_app_providers()) == "not_running"

    def test_an_app_that_quits_mid_check_is_not_running(self, app):
        app.drop = "list_model_providers"

        assert copilot_app.status(_app_providers()) == "not_running"


def test_retries_while_the_app_applies_another_change(app):
    seed(app, _app_providers())
    app.busy = 2
    app.busy_kinds = ("delete_model_provider",)

    assert copilot_app.unregister() is True
    assert app.providers == {}


def test_a_refusal_stops_the_unregister(app):
    seed(app, _app_providers())
    app.fail = "delete_model_provider"

    with pytest.raises(copilot_app.CopilotAppError, match="refused delete_model_provider"):
        copilot_app.unregister()


def _stray_result(**fields):
    return {"type": "command_result", "data": {}, **fields}


def test_every_request_carries_a_request_id(app):
    seed(app, _app_providers())
    copilot_app.unregister()

    ids = [message.get("request_id") for message in app.received]
    assert all(ids)
    assert len(set(ids)) == len(ids)


@pytest.mark.parametrize(
    "stray",
    [_stray_result(request_id="ug-someone-else"), _stray_result()],
    ids=["another-request-id", "no-request-id"],
)
def test_a_reply_not_for_this_request_is_skipped(app, stray):
    app.before_reply["list_model_providers"] = [stray]
    seed(app, _app_providers())

    assert copilot_app.unregister() is True


def test_an_error_for_another_request_is_skipped(app):
    app.before_reply["list_model_providers"] = [
        {"type": "error", "message": "boom", "request_id": "ug-someone-else"}
    ]
    seed(app, _app_providers())

    assert copilot_app.unregister() is True


def test_an_error_without_a_request_id_answers_the_pending_request(app):
    app.before_reply["list_model_providers"] = [
        {"type": "error", "message": "Invalid message format"}
    ]

    with pytest.raises(copilot_app.CopilotAppError, match="Invalid message format"):
        copilot_app.unregister()


def test_an_error_from_before_a_request_does_not_answer_it(app):
    app.before_hello = [{"type": "error", "message": "Invalid message format"}]
    seed(app, _app_providers())

    assert copilot_app.unregister() is True


def test_an_app_that_quits_mid_unregister_is_not_running(app):
    seed(app, _app_providers())
    app.drop = "delete_model_provider"

    with pytest.raises(copilot_app.CopilotAppNotRunning):
        copilot_app.unregister()


def test_a_closed_app_is_not_running(tmp_path, monkeypatch):
    home = tmp_path / ".copilot"
    monkeypatch.setenv("COPILOT_HOME", str(home))

    with pytest.raises(copilot_app.CopilotAppNotRunning):
        copilot_app.unregister()

    # The app leaves its run files behind when it exits.
    (home / "run").mkdir(parents=True)
    (home / "run" / "ws.port").write_text("1\n4242\n")
    (home / "run" / "ws.token").write_text("stale\n4242\n")
    with pytest.raises(copilot_app.CopilotAppNotRunning):
        copilot_app.unregister()


def test_a_port_file_of_an_exited_app_is_not_running(app, monkeypatch):
    monkeypatch.setattr(copilot_app, "process_running", lambda pid: False)

    assert copilot_app.status(_app_providers()) == "not_running"
    with pytest.raises(copilot_app.CopilotAppNotRunning):
        copilot_app.unregister()
    assert app.handshakes == []


def _app_1_4_2_run_files(pid_file):
    run = copilot_app.copilot_home() / "run"
    (run / "ws.port").write_text((run / "ws.port").read_text().split()[0])
    (run / "app-owner.pid").write_text(pid_file)


def test_an_exited_app_named_by_app_owner_pid_is_not_running(app, monkeypatch):
    _app_1_4_2_run_files("4242")
    checked = []
    monkeypatch.setattr(copilot_app, "process_running", lambda pid: checked.append(pid) or False)

    with pytest.raises(copilot_app.CopilotAppNotRunning):
        copilot_app.unregister()
    assert checked == [4242]
    assert app.handshakes == []


def test_an_unreadable_app_owner_pid_still_connects(app, monkeypatch):
    _app_1_4_2_run_files("not a pid")
    monkeypatch.setattr(copilot_app, "process_running", lambda pid: False)

    assert copilot_app.unregister() is False


def test_the_token_is_url_encoded(app):
    app.token = "a+b&c=d"
    (copilot_app.copilot_home() / "run" / "ws.token").write_text(f"{app.token}\n4242\n")

    assert copilot_app.unregister() is False


def test_a_rejected_token_is_an_app_error(app):
    (copilot_app.copilot_home() / "run" / "ws.token").write_text("wrong\n4242\n")

    with pytest.raises(copilot_app.CopilotAppError, match="refused ug"):
        copilot_app.unregister()


def test_status_waits_only_briefly_for_a_hung_app(app, monkeypatch):
    waits = []
    real_next = copilot_app._AppConnection._next

    def next_message(self, deadline):
        waits.append(deadline - copilot_app.time.monotonic())
        return real_next(self, deadline)

    monkeypatch.setattr(copilot_app._AppConnection, "_next", next_message)
    copilot_app.status(copilot_app.desired_providers(WORKSPACE, CATALOG).app)

    assert waits and max(waits) <= copilot_app._STATUS_TIMEOUT_S
