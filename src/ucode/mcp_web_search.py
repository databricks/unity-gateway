"""Stdio MCP server exposing a `web_search` tool backed by a Databricks-hosted
GPT model's native Responses API web search.

Claude Code on Databricks doesn't have working web search (the built-in
`WebSearch` tool talks to Anthropic's hosted infra, not the gateway). This
server bridges the gap: it advertises a single MCP tool, and on call it
forwards the query to the workspace's Responses API with
`tools: [{"type": "web_search"}]`, returning the model's text output.

Speaks MCP JSON-RPC 2.0 over stdio (newline-delimited JSON).
"""

from __future__ import annotations

import json
import os
import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

from ucode.databricks import get_databricks_token

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "ucode-web-search"
SERVER_VERSION = "0.1.0"
PROVIDER_ENV = "UCODE_CLAUDE_WEB_SEARCH_PROVIDER"
MANAGED_ENTRY_FLAG = "--managed-by-ucode"
EXTERNAL_PROVIDER_OVERRIDE_FLAG = "--external-provider-override"
AUTOMATIC_PROVIDER = "external-if-safe"
_MAX_CONCURRENT_SEARCHES = 4


def external_provider_selected() -> bool:
    """Launchers opt out of ug's generated search without changing shared configuration."""
    value = os.environ.get(PROVIDER_ENV, "ucode")
    if value not in ("ucode", "external", AUTOMATIC_PROVIDER):
        raise RuntimeError(
            f"{PROVIDER_ENV} must be 'ucode', 'external', or '{AUTOMATIC_PROVIDER}', got {value!r}."
        )
    return value != "ucode"


def capabilities() -> dict[str, Any]:
    return {
        "external_provider_contract": 1,
        "provider_env": PROVIDER_ENV,
        "managed_entry_flag": MANAGED_ENTRY_FLAG,
        "automatic_provider": AUTOMATIC_PROVIDER,
    }


TOOL_NAME = "web_search"
TOOL_DESCRIPTION = (
    "Search the web for up-to-date public information, current events, "
    "real-time facts, and recent data. Use this when the user's question "
    "requires information beyond your training data."
)
TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "The search query.",
        },
    },
    "required": ["query"],
}


def _tool_descriptor() -> dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "description": TOOL_DESCRIPTION,
        "inputSchema": TOOL_INPUT_SCHEMA,
    }


def _result(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _tool_error(text: str) -> dict[str, Any]:
    """An MCP tool-call result with isError=true. Different from JSON-RPC errors:
    tool-level failures should be returned as results so the model can see and
    react to them, not as protocol errors that abort the call."""
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _valid_request_id(value: Any) -> bool:
    # Booleans alias integer dictionary keys but are not JSON-RPC request IDs.
    return value is None or (isinstance(value, (str, int, float)) and not isinstance(value, bool))


def _extract_response_text(payload: dict[str, Any]) -> str:
    """Walk a Responses API payload and concatenate all `output_text` content
    from `message`-type output items. Skips reasoning, tool-call, and other
    item types — we only want the final user-facing answer."""
    parts: list[str] = []
    for item in payload.get("output", []) or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []) or []:
            if isinstance(content, dict) and content.get("type") == "output_text":
                text = content.get("text")
                if isinstance(text, str):
                    parts.append(text)
    return "\n".join(parts).strip()


def _call_responses_api(query: str) -> dict[str, Any]:
    """POST to the Databricks Codex (Responses API) gateway and return the
    parsed JSON payload. Raises RuntimeError on any failure with a message
    suitable for surfacing as a tool error."""
    workspace = os.environ.get("DATABRICKS_HOST", "").strip()
    model = os.environ.get("UCODE_WEB_SEARCH_MODEL", "").strip()
    profile = os.environ.get("DATABRICKS_CONFIG_PROFILE", "").strip() or None
    if not workspace:
        raise RuntimeError("DATABRICKS_HOST env var is not set.")
    if not model:
        raise RuntimeError("UCODE_WEB_SEARCH_MODEL env var is not set.")

    try:
        token = get_databricks_token(workspace, profile)
    except RuntimeError as exc:
        raise RuntimeError(f"Failed to acquire Databricks token: {exc}") from exc

    body = json.dumps(
        {
            "model": model,
            "input": [{"role": "user", "content": query}],
            "tools": [{"type": "web_search"}],
            "store": False,
        }
    ).encode("utf-8")

    request = urllib_request.Request(
        f"{workspace.rstrip('/')}/ai-gateway/codex/v1/responses",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib_request.urlopen(request, timeout=180) as response:
            raw = response.read().decode("utf-8")
    except urllib_error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:500]
        except Exception:
            pass
        raise RuntimeError(f"Responses API returned HTTP {exc.code}: {detail}") from exc
    except urllib_error.URLError as exc:
        raise RuntimeError(f"Responses API request failed: {exc.reason}") from exc

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Responses API returned non-JSON payload: {exc}") from exc


def _handle_tools_call(arguments: dict[str, Any]) -> dict[str, Any]:
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        return _tool_error("`query` must be a non-empty string.")
    try:
        payload = _call_responses_api(query)
    except RuntimeError as exc:
        return _tool_error(str(exc))

    text = _extract_response_text(payload)
    if not text:
        return _tool_error("Web search returned no text output.")
    return {"content": [{"type": "text", "text": text}]}


def _handle_request(req: dict[str, Any], *, search_enabled: bool = True) -> dict[str, Any] | None:
    """Dispatch a single JSON-RPC request. Returns the response dict, or None
    for notifications (which must not produce a response per JSON-RPC spec)."""
    method = req.get("method")
    req_id = req.get("id")
    params = req.get("params") or {}
    is_notification = "id" not in req

    if method == "initialize":
        return _result(
            req_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return _result(req_id, {"tools": [_tool_descriptor()] if search_enabled else []})
    if method == "tools/call":
        if params.get("name") != TOOL_NAME:
            return _error(req_id, -32602, f"Unknown tool: {params.get('name')!r}")
        if not search_enabled:
            return _result(
                req_id,
                _tool_error(
                    "This launch uses an external search provider; ug search is unavailable."
                ),
            )
        return _result(req_id, _handle_tools_call(params.get("arguments") or {}))

    if is_notification:
        return None
    return _error(req_id, -32601, f"Method not found: {method!r}")


def serve(
    stdin=None,
    stdout=None,
    *,
    managed_by_ucode: bool = False,
    external_provider_override: bool = False,
) -> None:
    """Read newline-delimited JSON-RPC requests from stdin, write responses to
    stdout. Drain accepted searches at EOF. Injectable streams for testing."""
    in_stream = stdin if stdin is not None else sys.stdin
    out_stream = stdout if stdout is not None else sys.stdout
    # A copied registration retains its marker. Only a verified launch override
    # may suppress it; inherited provider selection alone cannot establish ownership.
    search_enabled = not (
        managed_by_ucode and external_provider_override and external_provider_selected()
    )

    response_lock = threading.Lock()
    pending: dict[Any, Future[dict[str, Any] | None]] = {}

    def write_response(response: dict[str, Any] | None) -> None:
        if response is not None:
            out_stream.write(json.dumps(response) + "\n")
            out_stream.flush()

    def finish_request(req_id: Any, future: Future[dict[str, Any] | None]) -> None:
        try:
            response = future.result()
        except Exception:
            response = _error(req_id, -32603, "Internal error")
        with response_lock:
            # Cancellation or ID reuse must not deliver an old worker's response.
            if pending.get(req_id) is future:
                del pending[req_id]
                write_response(response)

    # Keep blocking auth/HTTP off the input loop, including while all workers are busy.
    executor = ThreadPoolExecutor(max_workers=_MAX_CONCURRENT_SEARCHES)
    try:
        for line in in_stream:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                with response_lock:
                    write_response(_error(None, -32700, "Parse error"))
                continue

            if not isinstance(req, dict) or ("id" in req and not _valid_request_id(req["id"])):
                with response_lock:
                    write_response(_error(None, -32600, "Invalid Request"))
                continue

            if "id" not in req:
                if req.get("method") == "notifications/cancelled":
                    params = req.get("params")
                    if (
                        isinstance(params, dict)
                        and "requestId" in params
                        and _valid_request_id(params["requestId"])
                    ):
                        with response_lock:
                            future = pending.pop(params["requestId"], None)
                        if future is not None:
                            # Running calls retain their worker until HTTP returns.
                            # cancel() invokes callbacks synchronously, outside our lock.
                            future.cancel()
                continue

            if req.get("method") == "tools/call":
                req_id = req["id"]
                with response_lock:
                    future = executor.submit(_handle_request, req, search_enabled=search_enabled)
                    pending[req_id] = future
                future.add_done_callback(lambda done, req_id=req_id: finish_request(req_id, done))
            else:
                response = _handle_request(req, search_enabled=search_enabled)
                with response_lock:
                    write_response(response)
        executor.shutdown(wait=True)
    except BaseException:
        with response_lock:
            pending.clear()
        # Cancelling queued futures runs their callbacks; do not hold the lock.
        executor.shutdown(wait=False, cancel_futures=True)
        executor.shutdown(wait=True)
        raise
