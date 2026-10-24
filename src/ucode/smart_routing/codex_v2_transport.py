"""Alias Codex's reserved collaboration namespace to request readable assignments."""

from __future__ import annotations

import copy
import json
import re
from typing import Any

NATIVE_NAMESPACE = "collaboration"
WIRE_NAMESPACE = "ucode_collaboration"
PLAINTEXT_TOOLS = frozenset({"spawn_agent", "send_message", "followup_task"})
_FERNET_TOKEN_RE = re.compile(r"^gAAAAA[A-Za-z0-9_-]+={0,2}$")


def prepare_request(body: bytes) -> tuple[bytes, bool]:
    """Rewrite tool definitions and plaintext history; leave encrypted history intact."""
    try:
        payload = json.loads(body)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return body, False
    if not isinstance(payload, dict):
        return body, False

    changed = _rewrite_tool_list(payload.get("tools"))

    input_items = payload.get("input")
    if isinstance(input_items, list):
        encrypted_call_ids = {
            item["call_id"]
            for item in input_items
            if isinstance(item, dict)
            and isinstance(item.get("call_id"), str)
            and _is_encrypted_replay(item)
        }
        for item in input_items:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "additional_tools":
                changed |= _rewrite_tool_list(item.get("tools"))
            elif item.get("type") in {"function_call", "function_call_output"}:
                if item.get("call_id") not in encrypted_call_ids:
                    changed |= _rewrite_replay_item(item)

    if not changed:
        return body, False
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), True


def transform_response_event(event: dict[str, Any]) -> dict[str, Any]:
    """Restore native calls, rejecting opaque assignments before hook dispatch."""
    if not isinstance(event, dict):
        raise TypeError("response event must be a JSON object")

    result = copy.deepcopy(event)
    item = result.get("item")
    if isinstance(item, dict):
        _rewrite_response_item(
            item, validate_arguments=result.get("type") != "response.output_item.added"
        )

    for response in (result, result.get("response")):
        if not isinstance(response, dict):
            continue
        output = response.get("output")
        if isinstance(output, list):
            for output_item in output:
                if isinstance(output_item, dict):
                    _rewrite_response_item(output_item, validate_arguments=True)
    return result


def _check_wire_namespace(item: dict[str, Any]) -> None:
    if item.get("namespace") == WIRE_NAMESPACE or (
        item.get("type") == "namespace" and item.get("name") == WIRE_NAMESPACE
    ):
        raise ValueError(f"request already contains reserved namespace {WIRE_NAMESPACE!r}")


def _rewrite_tool_list(tools: Any) -> bool:
    if not isinstance(tools, list):
        return False
    changed = False
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        _check_wire_namespace(tool)
        if tool.get("type") != "namespace" or tool.get("name") != NATIVE_NAMESPACE:
            continue
        tool["name"] = WIRE_NAMESPACE
        functions = tool.get("tools")
        changed = True
        if isinstance(functions, list):
            for function in functions:
                if isinstance(function, dict) and function.get("name") in PLAINTEXT_TOOLS:
                    _remove_message_encrypted_marker(function)
    return changed


def _remove_message_encrypted_marker(tool: dict[str, Any]) -> None:
    parameters = tool.get("parameters")
    if not isinstance(parameters, dict):
        return
    properties = parameters.get("properties")
    if not isinstance(properties, dict):
        return
    message = properties.get("message")
    if isinstance(message, dict):
        message.pop("encrypted", None)


def _is_encrypted_replay(item: dict[str, Any]) -> bool:
    return (
        item.get("namespace") == NATIVE_NAMESPACE
        and item.get("type") == "function_call"
        and (
            ("encrypted_function_args" in item and item["encrypted_function_args"] != [])
            or _contains_opaque_message(item.get("arguments"))
        )
    )


def _rewrite_replay_item(item: dict[str, Any]) -> bool:
    _check_wire_namespace(item)
    if item.get("namespace") != NATIVE_NAMESPACE or _is_encrypted_replay(item):
        # Old calls have already executed. Preserve their wire representation;
        # only new responses need to expose an assignment to the routing hook.
        return False
    is_function_call = item.get("type") == "function_call"
    item["namespace"] = WIRE_NAMESPACE
    if is_function_call and item.get("name") in PLAINTEXT_TOOLS:
        if item.get("encrypted_function_args") == []:
            del item["encrypted_function_args"]
    return True


def _rewrite_response_item(item: dict[str, Any], *, validate_arguments: bool) -> None:
    namespace = item.get("namespace")
    if namespace == NATIVE_NAMESPACE:
        raise ValueError("response returned native collaboration namespace")
    if namespace != WIRE_NAMESPACE:
        return
    if item.get("type") != "function_call":
        raise ValueError("wire collaboration response is not a function call")
    item["namespace"] = NATIVE_NAMESPACE
    if item.get("name") not in PLAINTEXT_TOOLS:
        return
    if item.get("encrypted_function_args", []) != []:
        raise ValueError("wire collaboration response contains encrypted function arguments")
    if validate_arguments:
        _validate_plaintext_arguments(item.get("arguments"))
    item["encrypted_function_args"] = []


def _contains_opaque_message(arguments: Any) -> bool:
    if isinstance(arguments, str):
        stripped = arguments.strip()
        if _FERNET_TOKEN_RE.fullmatch(stripped):
            return True
        try:
            arguments = json.loads(stripped)
        except json.JSONDecodeError:
            return False
    if isinstance(arguments, dict):
        message = arguments.get("message")
        return isinstance(message, str) and bool(_FERNET_TOKEN_RE.fullmatch(message.strip()))
    return False


def _validate_plaintext_arguments(arguments: Any) -> None:
    if not isinstance(arguments, str) or not arguments.strip():
        raise ValueError("wire collaboration response has empty arguments")
    if _contains_opaque_message(arguments):
        raise ValueError("wire collaboration response contains an opaque message")
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError as exc:
        raise ValueError("wire collaboration response arguments are not valid JSON") from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("message"), str):
        raise ValueError("wire collaboration response arguments lack a string message")
    if not parsed["message"].strip():
        raise ValueError("wire collaboration response arguments have an empty message")
