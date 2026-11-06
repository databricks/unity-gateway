"""Codex request compatibility and routing attribution without reloading conversations."""

from __future__ import annotations

import copy
import json
import os
import secrets
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from ucode import gateway_proxy
from ucode.config_io import APP_DIR
from ucode.constants import (
    ENABLE_SMART_ROUTING_ENV_VAR,
    ENABLE_SUBAGENT_ROUTING_ENV_VAR,
    LOOPBACK_HOST,
    SMART_ROUTER_RECIPE_HEADER,
)
from ucode.custom_oauth import launch_token
from ucode.databricks import classify_model_family
from ucode.smart_routing.session_env import _read

PROXY_PROVIDER = "DatabricksSmartRouter"
PROXY_KEY_ENV = "UG_CODEX_ROUTING_PROXY_KEY"


def _translate_oss_agent_messages(payload: dict) -> None:
    if classify_model_family(payload.get("model", "")) != "oss":
        return
    items = payload.get("input")
    if not isinstance(items, list):
        return
    for index, item in enumerate(items):
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            continue
        content = item.get("content")
        if not isinstance(content, list) or not content:
            raise gateway_proxy.RequestRewriteError(
                400,
                "This model needs plaintext Codex agent messages. Upgrade Codex and start a new session.",
            )
        # The gateway's OSS translator drops unknown item types, including native agent_message.
        recipient = item.get("recipient")
        address = (
            [{"type": "input_text", "text": f"Recipient: {recipient}\n"}]
            if isinstance(recipient, str) and recipient
            else []
        )
        items[index] = {"type": "message", "role": "user", "content": address + content}


class RoutingAttribution:
    def __init__(self, controls: Path, recipe: str):
        if not isinstance(recipe, str) or not recipe.strip():
            raise ValueError("Codex smart routing requires a nonempty launch recipe")
        self.controls = controls
        self.recipe = recipe

    def __call__(self, path: str, body: bytes | None) -> tuple[bytes | None, dict[str, str]]:
        try:
            overrides = _read(self.controls)
        except (OSError, UnicodeError, ValueError) as exc:
            raise gateway_proxy.RequestRewriteError(
                503, "Cannot read Smart Router session controls. Restart `ug codex`."
            ) from exc
        disabled = all(
            overrides.get(key) == "0"
            for key in (ENABLE_SMART_ROUTING_ENV_VAR, ENABLE_SUBAGENT_ROUTING_ENV_VAR)
        )
        recipe = "DISABLED" if disabled else self.recipe
        headers = {SMART_ROUTER_RECIPE_HEADER: recipe}
        if not body or not path.split("?", 1)[0].endswith(("/responses", "/responses/compact")):
            return body, headers
        try:
            payload = json.loads(body)
            metadata = payload.setdefault("client_metadata", {})
            raw = metadata.get("x-codex-turn-metadata", "{}")
            turn = json.loads(raw) if isinstance(raw, str) else raw
            turn["smart_router_recipe_name"] = recipe
            metadata["x-codex-turn-metadata"] = json.dumps(turn) if isinstance(raw, str) else turn
            _translate_oss_agent_messages(payload)
        except (ValueError, TypeError, AttributeError) as exc:
            raise gateway_proxy.RequestRewriteError(400, "Invalid Codex request metadata.") from exc
        return json.dumps(payload).encode(), headers


def _log_refresh_failure(exc: BaseException) -> None:
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        with (APP_DIR / "codex-gateway-proxy.log").open("a", encoding="utf-8") as handle:
            handle.write(
                f"Databricks token refresh failed ({type(exc).__name__}). Sign in again.\n"
            )
    except OSError:
        pass


@contextmanager
def session_proxy(state: dict, overlay: dict, controls: Path) -> Iterator[tuple[dict, dict]]:
    provider = copy.deepcopy(overlay["model_providers"][overlay["model_provider"]])
    headers = provider.get("http_headers", {})
    recipe = next(
        (
            value
            for name, value in headers.items()
            if name.casefold() == SMART_ROUTER_RECIPE_HEADER.casefold()
        ),
        "",
    )
    rewrite = RoutingAttribution(controls, recipe)
    path = urlsplit(provider["base_url"]).path.rstrip("/")
    secret = secrets.token_urlsafe(32)
    workspace = state["workspace"]
    server, cache, client = gateway_proxy.start_gateway_auth_proxy(
        workspace,
        lambda force: launch_token(state, workspace, force_refresh=force),
        session_secret=secret,
        added_headers=headers,
        sign_in_hint=f"Run `databricks auth login --host {workspace}`, then restart `ug codex`.",
        on_refresh_failure=_log_refresh_failure,
        allowed_path_prefixes=(path + "/",),
        request_rewriter=rewrite,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        provider.pop("auth", None)
        provider["env_key"] = PROXY_KEY_ENV
        provider["base_url"] = f"http://{LOOPBACK_HOST}:{server.server_address[1]}{path}"
        provider["supports_websockets"] = False
        config = copy.deepcopy(overlay)
        config["model_provider"] = PROXY_PROVIDER
        config["model_providers"] = {PROXY_PROVIDER: provider}
        yield config, {**os.environ, PROXY_KEY_ENV: secret}
    finally:
        cache.stop()
        server.shutdown()
        server.server_close()
        client.close()
        thread.join(timeout=5)
