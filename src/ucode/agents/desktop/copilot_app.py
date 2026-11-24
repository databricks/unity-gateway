"""Registers ug's model providers in the GitHub Copilot desktop app.

The app lists models in its picker only for providers written through the local WebSocket API its
own window uses. ug's Copilot plugin (``copilot_app_plugin``) writes them there whenever the app
runs, then re-supplies the same provider and model ids in every session and serves them from
the AI Gateway, so both come from :func:`desired_providers`. This module also talks to the running
app directly, to check the rows and to delete them at revert. The app publishes the port and a
per-launch token in ``~/.copilot/run/``. The providers have fixed ids, so revert deletes them by
id with no saved state.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode

from ucode.agents.copilot import (
    WIRE_ANTHROPIC,
    WIRE_COMPLETIONS,
    WIRE_RESPONSES,
    CopilotModel,
    catalog_model_limits,
    copilot_catalog_model_id,
    model_labels,
)
from ucode.agents.desktop.copilot_app_plugin import (
    AppProviderRows,
    PluginModel,
    PluginProvider,
    Wire,
    copilot_settings_path,
)
from ucode.constants import MODEL_PROVIDER_SERVICE_HEADER
from ucode.os_compatibility.system_cross_os import process_running
from ucode.ui import print_warning

# The app only lists models for an Anthropic provider that has a key. The plugin's provider
# replaces it with a bearer token, so it is never sent.
PLACEHOLDER_KEY = "ug-placeholder-not-a-secret"
_ID_NAMESPACE = uuid.UUID("6f1c2a4e-5d1b-4c39-9a57-0d2b8e7f4a11")
_REQUEST_TIMEOUT_S = 15
# `ug status` reads the app on every run, so a hung app may hold it up only briefly.
_STATUS_TIMEOUT_S = 3

AppStatus = Literal["registered", "outdated", "not_registered", "not_running"]


class CopilotAppError(RuntimeError):
    """The app refused, didn't understand or didn't answer a request. ug sent nothing after it,
    but requests already sent may have been applied."""


class CopilotAppNotRunning(CopilotAppError):
    """The app isn't running, so its providers can't be changed now."""


@dataclass(frozen=True)
class _ProviderSpec:
    key: str
    name: str
    wire: Wire


_PROVIDERS = (
    _ProviderSpec("claude", "Databricks Claude", WIRE_ANTHROPIC),
    _ProviderSpec("gpt", "Databricks GPT", WIRE_RESPONSES),
    _ProviderSpec("open-models", "Databricks Open Models", WIRE_COMPLETIONS),
)


def _provider_id(key: str) -> str:
    return str(uuid.uuid5(_ID_NAMESPACE, f"provider/{key}"))


def _model_row_id(key: str, model_id: str) -> str:
    return str(uuid.uuid5(_ID_NAMESPACE, f"model/{key}/{model_id}"))


@dataclass(frozen=True)
class AppProvider:
    """One provider as the app's WebSocket API stores it, with its model rows."""

    key: str
    name: str
    kind: str
    settings: dict[str, str]
    secret: dict[str, str] | None
    models: tuple[dict[str, Any], ...]

    @property
    def id(self) -> str:
        return _provider_id(self.key)

    def model_row_id(self, model_id: str) -> str:
        return _model_row_id(self.key, model_id)

    def rows(self) -> AppProviderRows:
        """The provider and its model rows as the plugin writes them to the app."""
        return AppProviderRows(
            provider={
                "id": self.id,
                "name": self.name,
                "kind": self.kind,
                "settings": self.settings,
            },
            secret=self.secret,
            models=tuple(
                {**row, "id": self.model_row_id(row["modelId"]), "providerId": self.id}
                for row in self.models
            ),
        )


@dataclass(frozen=True)
class DesiredProviders:
    """The app's providers and the plugin's, which must carry identical ids."""

    app: tuple[AppProvider, ...]
    plugin: tuple[PluginProvider, ...]
    # Gateway headers the plugin must add for these providers, beside ug's usual ones.
    headers: dict[str, str]

    @property
    def plugin_app(self) -> tuple[AppProviderRows, ...]:
        """``PluginConfig.app_providers``: the rows the plugin writes to the app."""
        return tuple(provider.rows() for provider in self.app)


# ``PluginConfig.owned_provider_ids``: every provider id ug may have written to the app.
OWNED_PROVIDER_IDS = tuple(_provider_id(spec.key) for spec in _PROVIDERS)


def copilot_home() -> Path:
    return copilot_settings_path().parent


def _base_path(wire: Wire, provider_service: str | None) -> str:
    if wire == WIRE_ANTHROPIC:
        return "/ai-gateway/anthropic"
    return "/ai-gateway/openai/v1" if provider_service else "/ai-gateway/mlflow/v1"


def _own_model_id(model: str) -> str:
    """A row id for gateway model ``model`` that can't be a Copilot catalog id: the app applies its
    own settings to a row with a catalog model's id, such as ``reasoning_effort`` for ``kimi-k3``,
    over the row's."""
    return model if model.count(".") >= 2 else f"databricks.{model}"


# The app's picker lists ug's rows next to GitHub's own models, some with the same names.
APP_LABEL_SUFFIX = " (Databricks)"


def _model_rows(
    catalog: Iterable[CopilotModel], wire: str, *, provider_service: str | None
) -> list[dict[str, Any]]:
    """The app's model rows for ``catalog``'s models on ``wire``, labelled as Databricks models.

    A Claude row's id is Copilot's catalog id for the model behind it, which is what makes the app
    apply Claude's request settings, and its wire model is the gateway's id. Other rows get an id
    no Copilot catalog model has (see :func:`_own_model_id`). Ids the app would see twice keep the
    first, with a warning."""
    models = [model for model in catalog if model.wire == wire]
    labels = model_labels(models, provider_service=provider_service)
    rows: dict[str, dict[str, Any]] = {}
    for model in models:
        if wire == WIRE_ANTHROPIC:
            model_id = copilot_catalog_model_id(model.catalog_name)
            if model_id is None:
                print_warning(
                    f"Skipping {model.id} in the GitHub Copilot app: ug couldn't tell which "
                    "Claude model it serves."
                )
                continue
        else:
            model_id = _own_model_id(model.id)
        if model_id in rows:
            print_warning(
                f"Skipping {model.id} in the GitHub Copilot app: it shows as {model_id}, like "
                f"{rows[model_id]['wireModel']}."
            )
            continue
        row: dict[str, Any] = {
            "modelId": model_id,
            "wireModel": model.id,
            "displayName": labels[model] + APP_LABEL_SUFFIX,
            "supportsVision": wire == WIRE_ANTHROPIC,
        }
        if wire == WIRE_RESPONSES:
            row["wireApiOverride"] = "responses"
        if limits := catalog_model_limits(model):
            row["maxPromptTokens"], row["maxOutputTokens"] = limits
        rows[model_id] = row
    return list(rows.values())


def _plugin_model(row: dict[str, Any], wire: Wire) -> PluginModel:
    limits = None
    if "maxPromptTokens" in row:
        limits = (row["maxPromptTokens"], row["maxOutputTokens"])
    return PluginModel(
        row["modelId"],
        row["wireModel"],
        row["displayName"],
        limits,
        row["supportsVision"],
        # Every model on Chat Completions mishandles Copilot's `reasoning_effort`: OpenAI and Azure
        # reject it for GPT before 5, GLM 5.3 rejects `none`, and GLM and Kimi think less at `high`
        # than without it.
        reasoning_effort=False if wire == WIRE_COMPLETIONS else None,
    )


def desired_providers(
    workspace: str, catalog: Iterable[CopilotModel], *, provider_service: str | None = None
) -> DesiredProviders:
    """ug's providers on ``workspace``'s AI Gateway for ``catalog`` (see
    ``vscode.copilot_models``), each with its models; a wire with no models gets no provider."""
    catalog = list(catalog)
    base = workspace.rstrip("/")
    app: list[AppProvider] = []
    plugin: list[PluginProvider] = []
    for spec in _PROVIDERS:
        rows = _model_rows(catalog, spec.wire, provider_service=provider_service)
        if not rows:
            continue
        base_path = _base_path(spec.wire, provider_service)
        anthropic = spec.wire == WIRE_ANTHROPIC
        provider = AppProvider(
            spec.key,
            spec.name,
            "anthropic" if anthropic else "custom",
            {
                "baseUrl": base + base_path,
                "wireApi": "completions" if anthropic else spec.wire,
                "authKind": "api_key" if anthropic else "none",
                "headersJson": "{}",
            },
            {"kind": "apiKey", "value": PLACEHOLDER_KEY} if anthropic else None,
            tuple(rows),
        )
        app.append(provider)
        plugin.append(
            PluginProvider(
                id=provider.id,
                wire=spec.wire,
                base_path=base_path,
                models=tuple(_plugin_model(row, spec.wire) for row in rows),
            )
        )
    headers = {MODEL_PROVIDER_SERVICE_HEADER: provider_service} if provider_service else {}
    return DesiredProviders(tuple(app), tuple(plugin), headers)


def state_providers(
    state: dict, *, provider: str | None = None, location: str | None = None
) -> DesiredProviders:
    """:func:`desired_providers` for the models VS Code gets from the same source: a Model Provider
    Service (``provider``), a ``<catalog>.<schema>`` (``location``) or the discovered catalog."""
    from ucode.agents.vscode import copilot_models

    return desired_providers(
        state["workspace"],
        copilot_models(state, provider, location, agent="the GitHub Copilot app"),
        provider_service=provider,
    )


def _app_pid(run: Path) -> int | None:
    # The app writes its pid to app-owner.pid; ws.port carries none in app 1.4.2.
    for name, index in (("app-owner.pid", 0), ("ws.port", 1)):
        try:
            pid = int((run / name).read_text().split()[index])
        except (OSError, ValueError, IndexError):
            continue
        if pid > 1:
            return pid
    return None


def app_running(home: Path | None = None) -> bool:
    """Whether the app is open: it writes its run files under the Copilot home and leaves them
    behind on exit, so the pid in them has to be alive."""
    run = (home or copilot_home()) / "run"
    if not (run / "ws.port").is_file():
        return False
    pid = _app_pid(run)
    return pid is not None and process_running(pid)


class _AppConnection:
    """One connection to the app's WebSocket API, matching each reply to its request."""

    def __init__(
        self, home: Path, *, session_channels: bool = False, timeout: float = _REQUEST_TIMEOUT_S
    ) -> None:
        from websockets.exceptions import InvalidHandshake, InvalidStatus
        from websockets.sync.client import connect

        run = home / "run"
        try:
            port = int((run / "ws.port").read_text().split()[0])
            token = (run / "ws.token").read_text().split()[0]
        except (OSError, ValueError, IndexError):
            raise CopilotAppNotRunning("The GitHub Copilot app isn't running.") from None
        # The app leaves its run files behind when it exits, and another program may since have
        # taken the port.
        pid = _app_pid(run)
        if pid is not None and not process_running(pid):
            raise CopilotAppNotRunning("The GitHub Copilot app isn't running.")
        # With session channels, the app also sends every session's events, its window's chats
        # included.
        query = urlencode(
            {
                "token": token,
                "requester_id": "ug",
                "session_channels": "true" if session_channels else "false",
            }
        )
        url = f"ws://127.0.0.1:{port}/?{query}"
        try:
            # No Origin: the app refuses any origin but its own window.
            self._connection = connect(
                url, origin=None, compression=None, open_timeout=min(5, timeout)
            )
            self._ws = self._connection.__enter__()
        except InvalidStatus as exc:
            raise CopilotAppError(f"The GitHub Copilot app refused ug ({exc}).") from None
        except (OSError, TimeoutError, InvalidHandshake):
            raise CopilotAppNotRunning("The GitHub Copilot app isn't running.") from None
        self._timeout = timeout
        self._backlog: list[dict[str, Any]] = []
        try:
            hello = self._wait(lambda message: message.get("type") == "server_hello")
        except Exception:
            self.close()
            raise
        if hello is None:
            self.close()
            raise CopilotAppError("The GitHub Copilot app didn't greet ug.")

    def _next(self, deadline: float) -> dict[str, Any] | None:
        from websockets.exceptions import ConnectionClosed

        if self._backlog:
            return self._backlog.pop(0)
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                text = self._ws.recv(timeout=remaining)
            except TimeoutError:
                return None
            except ConnectionClosed:
                raise CopilotAppNotRunning(
                    "The GitHub Copilot app closed ug's connection."
                ) from None
            try:
                message = json.loads(text)
            except ValueError:
                continue
            if isinstance(message, dict):
                return message
        return None

    def next_message(self, timeout: float) -> dict[str, Any] | None:
        """The app's next message within ``timeout`` seconds, or None. Raises
        ``CopilotAppNotRunning`` once the app closes the connection."""
        return self._next(time.monotonic() + timeout)

    def _wait(self, matches, timeout: float | None = None) -> dict[str, Any] | None:
        deadline = time.monotonic() + (self._timeout if timeout is None else timeout)
        skipped: list[dict[str, Any]] = []
        try:
            while (message := self._next(deadline)) is not None:
                if matches(message):
                    return message
                skipped.append(message)
            return None
        finally:
            self._backlog = skipped + self._backlog

    def request(self, message: dict[str, Any], reply: str, **match: str) -> dict[str, Any]:
        """Send ``message`` and return the ``reply`` to it; ``match`` pins reply fields."""
        from websockets.exceptions import ConnectionClosed

        for attempt in range(6):
            request_id = f"ug-{uuid.uuid4().hex[:12]}"
            # Requests run one at a time, so nothing received before this send answers it; an
            # id-less error left over from earlier would otherwise read as a refusal.
            self._backlog.clear()
            try:
                self._ws.send(json.dumps({**message, "request_id": request_id}))
            except ConnectionClosed:
                raise CopilotAppNotRunning(
                    "The GitHub Copilot app closed ug's connection."
                ) from None

            def answers(candidate: dict[str, Any], request_id=request_id) -> bool:
                # The app echoes request_id on its replies, but not on errors about the
                # message's format, which then answer the pending request.
                if candidate.get("type") == "error":
                    return candidate.get("request_id") in (request_id, None)
                return (
                    candidate.get("request_id") == request_id
                    and candidate.get("type") == reply
                    and all(candidate.get(name) == value for name, value in match.items())
                )

            answer = self._wait(answers)
            if answer is None:
                raise CopilotAppError(f"The GitHub Copilot app didn't answer {message['type']}.")
            if answer.get("type") != "error":
                return answer
            error = str(answer.get("message", ""))
            # The app refuses a provider change while another one is being applied.
            if "still in progress" not in error or attempt == 5:
                raise CopilotAppError(f"The GitHub Copilot app refused {message['type']}: {error}")
            time.sleep(0.2 * 2**attempt)
        raise AssertionError("unreachable")

    def close(self) -> None:
        self._connection.__exit__(None, None, None)


def _providers(app: _AppConnection) -> dict[str, dict[str, Any]]:
    providers = app.request({"type": "list_model_providers"}, "model_providers_list").get(
        "providers"
    )
    if not isinstance(providers, list) or not all(
        isinstance(p, dict) and isinstance(p.get("id"), str) for p in providers
    ):
        raise CopilotAppError("The GitHub Copilot app listed its model providers in a new shape.")
    return {provider["id"]: provider for provider in providers}


def _models(app: _AppConnection, provider_id: str) -> list[dict[str, Any]]:
    models = app.request(
        {"type": "list_provider_models", "provider_id": provider_id}, "provider_models_list"
    ).get("models")
    if not isinstance(models, list):
        raise CopilotAppError("The GitHub Copilot app listed its models in a new shape.")
    return [
        m
        for m in models
        if isinstance(m, dict)
        and isinstance(m.get("id"), str)
        and m.get("providerId") == provider_id
    ]


def _connect(*, timeout: float = _REQUEST_TIMEOUT_S) -> _AppConnection:
    return _AppConnection(copilot_home(), timeout=timeout)


def _delete_provider(app: _AppConnection, provider_id: str) -> None:
    app.request(
        {"type": "delete_model_provider", "provider_id": provider_id},
        "model_provider_deleted",
        provider_id=provider_id,
    )


def unregister() -> bool:
    """Delete ug's providers from the running app (it also deletes their models and key).

    Returns whether any was there. Raises :class:`CopilotAppNotRunning` when the app is closed."""
    app = _connect()
    try:
        existing = _providers(app)
        removed = False
        for spec in _PROVIDERS:
            provider_id = _provider_id(spec.key)
            if provider_id in existing:
                _delete_provider(app, provider_id)
                removed = True
        return removed
    finally:
        app.close()


def _differs(listed: dict[str, Any], wanted: dict[str, Any]) -> bool:
    return any(name in listed and listed[name] != value for name, value in wanted.items())


def status(providers: Iterable[AppProvider]) -> AppStatus:
    """Whether the running app has ``providers`` and their models as ug's only providers, with the
    same names, settings and row fields (those the app lists back).

    ``outdated`` means it has some of ug's providers, but not these, or one is missing its key.
    Raises
    :class:`CopilotAppError` when the app refuses."""
    providers = list(providers)
    try:
        app = _connect(timeout=_STATUS_TIMEOUT_S)
    except CopilotAppNotRunning:
        return "not_running"
    try:
        existing = _providers(app)
        ours = {_provider_id(spec.key) for spec in _PROVIDERS} & set(existing)
        if not ours:
            return "not_registered"
        if ours != {provider.id for provider in providers}:
            return "outdated"
        for provider in providers:
            wanted = provider.rows()
            if _differs(existing[provider.id], dict(wanted.provider)):
                return "outdated"
            # Without its key, the app hides the provider's models from the picker.
            if wanted.secret is not None and not existing[provider.id].get("hasSecret"):
                return "outdated"
            listed = {model["id"]: model for model in _models(app, provider.id)}
            if set(listed) != {model["id"] for model in wanted.models}:
                return "outdated"
            if any(_differs(listed[model["id"]], dict(model)) for model in wanted.models):
                return "outdated"
        return "registered"
    except CopilotAppNotRunning:
        return "not_running"
    finally:
        app.close()
