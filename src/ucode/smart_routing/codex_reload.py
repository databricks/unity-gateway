"""Launch configuration and native resume continuation for routing restarts."""

from __future__ import annotations

import copy
import uuid
from pathlib import Path
from queue import Empty, SimpleQueue

from ucode.constants import (
    ENABLE_SMART_ROUTING_ENV_VAR,
    ENABLE_SUBAGENT_ROUTING_ENV_VAR,
    SMART_ROUTER_RECIPE_HEADER,
)
from ucode.smart_routing.session_env import read_session_environment


def routing_config(config: dict, enabled: bool) -> dict:
    result = copy.deepcopy(config)
    headers = result["model_providers"][result["model_provider"]].setdefault("http_headers", {})
    recipe = (
        next(
            (
                value
                for name, value in headers.items()
                if name.casefold() == SMART_ROUTER_RECIPE_HEADER.casefold()
            ),
            None,
        )
        if enabled
        else "DISABLED"
    )
    if not isinstance(recipe, str) or not recipe.strip():
        raise RuntimeError("Codex smart routing requires a nonempty launch recipe")
    for name in list(headers):
        if name.casefold() == SMART_ROUTER_RECIPE_HEADER.casefold():
            del headers[name]
    headers[SMART_ROUTER_RECIPE_HEADER] = recipe
    result.setdefault("responses_api_metadata", {})["smart_router_recipe_name"] = recipe
    return result


def session_routing_enabled(controls: Path) -> bool:
    overrides = read_session_environment(controls)
    return not all(
        overrides.get(key) == "0"
        for key in (ENABLE_SMART_ROUTING_ENV_VAR, ENABLE_SUBAGENT_ROUTING_ENV_VAR)
    )


class RestartContinuation:
    def __init__(self, notices: SimpleQueue[str]):
        self.notices = notices
        self.requests: dict[str | int, str] = {}
        self.root_id: str | None = None
        self.turn_id: str | None = None
        self.recipe: str | None = None

    def before_tui(self, message: dict) -> None:
        params = message.get("params", {})
        if (
            message.get("method") in ("thread/start", "thread/resume")
            and isinstance(message.get("id"), (str, int))
            and isinstance(params, dict)
            and not params.get("ephemeral")
        ):
            self.requests[message["id"]] = message["method"]

    def after_resume(self, message: dict) -> dict | None:
        identity = message.get("id")
        if not isinstance(identity, (str, int)):
            return None
        method = self.requests.pop(identity, None)
        result = message.get("result")
        if method is None or "error" in message or not isinstance(result, dict):
            return None
        thread = result.get("thread")
        if not isinstance(thread, dict):
            return None
        source = thread.get("source")
        if thread.get("parentThreadId") or isinstance(source, dict) and "subAgent" in source:
            return None
        thread_id = thread.get("id")
        if not isinstance(thread_id, str):
            return None
        if not self.notices.empty() and self.root_id is not None and thread_id != self.root_id:
            return None
        self.root_id = thread_id
        if method != "thread/resume":
            return None
        recipe = None
        while True:
            try:
                recipe = self.notices.get_nowait()
            except Empty:
                break
        if recipe is None:
            return None
        self.turn_id = f"ug-restart-{uuid.uuid4().hex}"
        self.recipe = recipe
        state = "off" if recipe == "DISABLED" else "on"
        return {
            "id": self.turn_id,
            "method": "turn/start",
            "params": {
                "threadId": thread_id,
                "input": [
                    {
                        "type": "text",
                        "text": (
                            f"Smart Router is now {state}. UG restarted the app-server to update request "
                            "attribution. Do not repeat the routing toggle command. Continue the previous "
                            "unfinished task. Existing children are saved but stopped and may be absent "
                            "from list_agents until reopened. Recover their task names and assignments "
                            "from earlier spawn_agent and followup_task calls in this conversation. Use "
                            "followup_task to resume every unfinished child assignment, even if the latest "
                            "user prompt only changed Smart Router. Do not spawn replacement "
                            "agents. Running commands were interrupted; inspect any completed effects before "
                            "retrying them. Only after checking children, if no unfinished parent or child "
                            "work remains, confirm the new state and stop."
                        ),
                    }
                ],
            },
        }

    def take_response(self, message: dict) -> bool:
        if self.turn_id is None or message.get("id") != self.turn_id:
            return False
        self.turn_id = None
        return True
