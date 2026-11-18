"""Registers ug's model providers in the GitHub Copilot desktop app.

The app has no import file or setting for model providers, so ug writes them through the local
WebSocket API the app's own window uses. The app must be running: it publishes the port and a
per-launch token in ``~/.copilot/run/``. The providers have fixed ids, so registering again
updates them in place and revert finds them without any state.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ucode.agents.copilot import (
    WIRE_ANTHROPIC,
    WIRE_RESPONSES,
    CopilotModel,
    catalog_model_limits,
    copilot_catalog_model_id,
    model_labels,
)
from ucode.ui import print_warning

# The app versions whose WebSocket messages ug was checked against; others get a warning.
VERIFIED_APP_VERSIONS = frozenset({"1.4.2"})
# The app only lists models for an Anthropic provider that has a key. The service drops it.
PLACEHOLDER_KEY = "ug-placeholder-not-a-secret"
_ID_NAMESPACE = uuid.UUID("6f1c2a4e-5d1b-4c39-9a57-0d2b8e7f4a11")
_REQUEST_TIMEOUT_S = 15


class CopilotAppError(RuntimeError):
    """The app refused or didn't understand a request; nothing more was changed."""


class CopilotAppNotRunning(CopilotAppError):
    """The app isn't running, so its providers can't be changed now."""


@dataclass(frozen=True)
class _Provider:
    key: str
    name: str
    kind: str
    settings: dict[str, str]
    secret: dict[str, str] | None
    models: list[dict[str, Any]]

    @property
    def id(self) -> str:
        return _provider_id(self.key)


def _provider_id(key: str) -> str:
    return str(uuid.uuid5(_ID_NAMESPACE, f"provider/{key}"))


def _model_id(key: str, model_id: str) -> str:
    return str(uuid.uuid5(_ID_NAMESPACE, f"model/{key}/{model_id}"))


_PROVIDER_KEYS = ("claude", "gpt")


def copilot_home() -> Path:
    return Path(os.environ.get("COPILOT_HOME") or Path.home() / ".copilot")


def _model_rows(
    catalog: Iterable[CopilotModel], wire: str, *, provider_service: str | None
) -> list[dict[str, Any]]:
    """The app's model rows for ``catalog``'s models on ``wire``.

    A Claude row's id is Copilot's catalog id, which is what makes the app apply Claude's request
    settings, and its wire model is the gateway's id. Ids the app would see twice keep the first."""
    models = [model for model in catalog if model.wire == wire]
    labels = model_labels(models, provider_service=provider_service)
    rows: dict[str, dict[str, Any]] = {}
    for model in models:
        if wire == WIRE_ANTHROPIC:
            model_id = copilot_catalog_model_id(model.id)
            if model_id is None:
                continue
        else:
            model_id = model.registry_id
        if model_id in rows:
            continue
        row: dict[str, Any] = {
            "modelId": model_id,
            "wireModel": model.id,
            "displayName": labels[model],
            "supportsVision": wire == WIRE_ANTHROPIC,
        }
        if wire == WIRE_RESPONSES:
            row["wireApiOverride"] = "responses"
        if limits := catalog_model_limits(model):
            row["maxPromptTokens"], row["maxOutputTokens"] = limits
        rows[model_id] = row
    return list(rows.values())


def desired_providers(
    base_url: str, catalog: Iterable[CopilotModel], *, provider_service: str | None = None
) -> list[_Provider]:
    """ug's two providers on the login service at ``base_url``, each with its models."""
    catalog = list(catalog)
    return [
        _Provider(
            "claude",
            "Databricks Claude",
            "anthropic",
            {
                "baseUrl": f"{base_url}/copilot/anthropic",
                "wireApi": "completions",
                "authKind": "api_key",
                "headersJson": "{}",
            },
            {"kind": "apiKey", "value": PLACEHOLDER_KEY},
            _model_rows(catalog, WIRE_ANTHROPIC, provider_service=provider_service),
        ),
        _Provider(
            "gpt",
            "Databricks GPT",
            "custom",
            {
                "baseUrl": f"{base_url}/copilot/responses/v1",
                "wireApi": "responses",
                "authKind": "none",
                "headersJson": "{}",
            },
            None,
            _model_rows(catalog, WIRE_RESPONSES, provider_service=provider_service),
        ),
    ]


class _AppConnection:
    """One connection to the app's WebSocket API, matching each reply to its request."""

    def __init__(self, home: Path) -> None:
        from websockets.exceptions import InvalidHandshake, InvalidStatus
        from websockets.sync.client import connect

        run = home / "run"
        try:
            # Each file holds the value, then the app's pid; a stale one names a closed port.
            port = int((run / "ws.port").read_text().split()[0])
            token = (run / "ws.token").read_text().split()[0]
        except (OSError, ValueError, IndexError):
            raise CopilotAppNotRunning("The GitHub Copilot app isn't running.") from None
        url = f"ws://127.0.0.1:{port}/?token={token}&requester_id=ug&session_channels=false"
        try:
            # No Origin: the app refuses any origin but its own window.
            self._connection = connect(url, origin=None, compression=None, open_timeout=5)
            self._ws = self._connection.__enter__()
        except InvalidStatus as exc:
            raise CopilotAppError(f"The GitHub Copilot app refused ug ({exc}).") from None
        except (OSError, TimeoutError, InvalidHandshake):
            raise CopilotAppNotRunning("The GitHub Copilot app isn't running.") from None
        self._backlog: list[dict[str, Any]] = []
        if self._wait(lambda message: message.get("type") == "server_hello") is None:
            self.close()
            raise CopilotAppError("The GitHub Copilot app didn't greet ug.")

    def _next(self, deadline: float) -> dict[str, Any] | None:
        if self._backlog:
            return self._backlog.pop(0)
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                text = self._ws.recv(timeout=remaining)
            except TimeoutError:
                return None
            try:
                message = json.loads(text)
            except ValueError:
                continue
            if isinstance(message, dict):
                return message
        return None

    def _wait(self, matches, timeout: float = _REQUEST_TIMEOUT_S) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
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
        for attempt in range(6):
            request_id = f"ug-{uuid.uuid4().hex[:12]}"
            self._ws.send(json.dumps({**message, "request_id": request_id}))

            def answers(candidate: dict[str, Any], request_id=request_id) -> bool:
                if candidate.get("request_id") not in (request_id, None):
                    return False
                if candidate.get("type") == "error":
                    return True
                return candidate.get("type") == reply and all(
                    candidate.get(name) == value for name, value in match.items()
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


def _version(app: _AppConnection) -> str | None:
    reply = app.request({"type": "get_healthcheck"}, "command_result")
    version = ((reply.get("data") or {}).get("app") or {}).get("version")
    return version if isinstance(version, str) else None


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
    return [m for m in models if isinstance(m, dict) and m.get("providerId") == provider_id]


def _connect() -> _AppConnection:
    app = _AppConnection(copilot_home())
    try:
        version = _version(app)
    except CopilotAppError:
        app.close()
        raise
    if version not in VERIFIED_APP_VERSIONS:
        print_warning(
            f"GitHub Copilot app {version or '(unknown version)'} is newer than ug knows; "
            "registering its Databricks providers anyway."
        )
    return app


def register(
    base_url: str, catalog: Iterable[CopilotModel], *, provider_service: str | None = None
) -> None:
    """Add or update ug's providers and their models in the running app.

    Raises :class:`CopilotAppNotRunning` when the app is closed, else :class:`CopilotAppError`
    when it refuses."""
    app = _connect()
    try:
        existing = _providers(app)
        for provider in desired_providers(base_url, catalog, provider_service=provider_service):
            current = existing.get(provider.id)
            message: dict[str, Any] = {
                "type": "upsert_model_provider",
                "provider": {
                    "id": provider.id,
                    "name": provider.name,
                    "kind": provider.kind,
                    "settings": provider.settings,
                },
            }
            if provider.secret and not (current and current.get("hasSecret")):
                message["secret"] = provider.secret
            app.request(message, "model_provider_upserted")
            wanted = {_model_id(provider.key, row["modelId"]) for row in provider.models}
            for model in _models(app, provider.id):
                if model.get("id") not in wanted:
                    app.request(
                        {"type": "delete_provider_model", "model_id": model["id"]},
                        "provider_model_deleted",
                        model_id=model["id"],
                    )
            for row in provider.models:
                app.request(
                    {
                        "type": "upsert_provider_model",
                        "model": {
                            **row,
                            "id": _model_id(provider.key, row["modelId"]),
                            "providerId": provider.id,
                        },
                    },
                    "provider_model_upserted",
                )
    finally:
        app.close()


def unregister() -> bool:
    """Delete ug's providers from the running app (it also deletes their models and key).

    Returns whether any was there. Raises :class:`CopilotAppNotRunning` when the app is closed."""
    app = _connect()
    try:
        existing = _providers(app)
        removed = False
        for key in _PROVIDER_KEYS:
            provider_id = _provider_id(key)
            if provider_id in existing:
                app.request(
                    {"type": "delete_model_provider", "provider_id": provider_id},
                    "model_provider_deleted",
                    provider_id=provider_id,
                )
                removed = True
        return removed
    finally:
        app.close()
