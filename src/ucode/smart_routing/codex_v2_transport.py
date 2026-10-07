"""Adapt native Codex v2 collaboration calls for a plaintext-capable wire route.

Native Codex keeps ``collaboration`` as the local namespace and uses an empty
``encrypted_function_args`` list to mark a readable response.  The gateway
alias makes the provider see an ordinary namespace whose ``message`` schema is
not reserved for encrypted arguments.  This module only transforms decoded
JSON values; transport framing, authentication, and stream management remain
owned by the caller.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

NATIVE_NAMESPACE = "collaboration"
WIRE_NAMESPACE = "ucode_collaboration"
PLAINTEXT_TOOLS = frozenset({"spawn_agent", "send_message", "followup_task"})
NATIVE_V2_TOOLS = frozenset(
    {
        "spawn_agent",
        "send_message",
        "followup_task",
        "wait_agent",
        "list_agents",
        "interrupt_agent",
    }
)
_FERNET_TOKEN_RE = re.compile(r"^gAAAAA[A-Za-z0-9_-]+={0,2}$")


def prepare_request(body: bytes) -> tuple[bytes, bool]:
    """Rewrite one JSON Responses request for the plaintext wire namespace.

    The original bytes are returned for non-JSON or non-v2 requests.  A request
    containing the reserved wire namespace before this adapter runs is rejected
    so an unrelated provider tool cannot be silently rewritten.
    """
    try:
        payload = json.loads(body)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return body, False
    if not isinstance(payload, dict):
        return body, False

    if _contains_wire_namespace(payload):
        raise ValueError(f"request already contains reserved namespace {WIRE_NAMESPACE!r}")

    changed = False
    tools = payload.get("tools")
    if isinstance(tools, list):
        changed |= _rewrite_tool_list(tools)

    input_items = payload.get("input")
    if isinstance(input_items, list):
        for item in input_items:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "additional_tools":
                item_tools = item.get("tools")
                if isinstance(item_tools, list):
                    changed |= _rewrite_tool_list(item_tools)
            elif item.get("type") in {"function_call", "function_call_output"}:
                changed |= _rewrite_replay_item(item)

    if not changed:
        return body, False
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), True


def transform_response_event(event: dict[str, Any]) -> dict[str, Any]:
    """Map provider response events back to native Codex v2 response items.

    ``response.output_item.added`` and ``response.output_item.done`` carry the
    item under ``item``.  A completed/full response may carry the same items in
    ``response.output``; both forms are adapted.  A plaintext-capable target
    must have no encryption marker or an empty marker.  Non-empty or malformed
    markers fail closed instead of handing ciphertext to native hooks.
    """
    if not isinstance(event, dict):
        raise TypeError("response event must be a JSON object")

    result = copy.deepcopy(event)
    item = result.get("item")
    if isinstance(item, dict):
        _rewrite_response_item(
            item, validate_arguments=result.get("type") != "response.output_item.added"
        )

    response = result.get("response")
    if isinstance(response, dict):
        output = response.get("output")
        if isinstance(output, list):
            for output_item in output:
                if isinstance(output_item, dict):
                    _rewrite_response_item(output_item, validate_arguments=True)

    output = result.get("output")
    if isinstance(output, list):
        for output_item in output:
            if isinstance(output_item, dict):
                _rewrite_response_item(output_item, validate_arguments=True)
    return result


def _contains_wire_namespace(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("namespace") == WIRE_NAMESPACE:
            return True
        if value.get("type") == "namespace" and value.get("name") == WIRE_NAMESPACE:
            return True
        return any(_contains_wire_namespace(child) for child in value.values())
    if isinstance(value, list):
        return any(_contains_wire_namespace(child) for child in value)
    return False


def _rewrite_tool_list(tools: list[Any]) -> bool:
    changed = False
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "namespace" and tool.get("name") == NATIVE_NAMESPACE:
            _validate_native_namespace_tools(tool)
            tool["name"] = WIRE_NAMESPACE
            _remove_plaintext_schema_markers(tool)
            changed = True
        elif tool.get("namespace") == NATIVE_NAMESPACE:
            _validate_native_tool_name(tool.get("name"))
            tool["namespace"] = WIRE_NAMESPACE
            if tool.get("name") in PLAINTEXT_TOOLS:
                _remove_message_encrypted_marker(tool)
            changed = True
    return changed


def _remove_plaintext_schema_markers(namespace: dict[str, Any]) -> None:
    tools = namespace.get("tools")
    if not isinstance(tools, list):
        return
    for tool in tools:
        if isinstance(tool, dict) and tool.get("name") in PLAINTEXT_TOOLS:
            _remove_message_encrypted_marker(tool)


def _validate_native_namespace_tools(namespace: dict[str, Any]) -> None:
    tools = namespace.get("tools")
    if not isinstance(tools, list):
        raise ValueError("native collaboration namespace has invalid tools")
    names: list[str] = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            raise ValueError("native collaboration namespace contains an unknown tool")
        name = tool.get("name")
        _validate_native_tool_name(name)
        names.append(name)
    if len(names) != len(set(names)):
        raise ValueError("native collaboration namespace contains duplicate tools")


def _validate_native_tool_name(name: Any) -> None:
    if name not in NATIVE_V2_TOOLS:
        raise ValueError(f"native collaboration namespace contains unknown tool {name!r}")


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


def _rewrite_replay_item(item: dict[str, Any]) -> bool:
    if item.get("namespace") != NATIVE_NAMESPACE:
        return False
    marker_present = "encrypted_function_args" in item
    marker = item.get("encrypted_function_args")
    if marker_present and marker != []:
        raise ValueError("native collaboration replay contains encrypted function arguments")
    is_function_call = item.get("type") == "function_call"
    if is_function_call:
        _validate_native_tool_name(item.get("name"))
        if _contains_opaque_message(item.get("arguments")):
            raise ValueError("native collaboration replay contains an opaque message")
    item["namespace"] = WIRE_NAMESPACE
    if is_function_call and item.get("name") in PLAINTEXT_TOOLS:
        # Empty is the native plaintext marker.  Non-empty markers were rejected
        # above rather than sent through the old opaque-message route.
        if marker == []:
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
    _validate_native_tool_name(item.get("name"))
    item["namespace"] = NATIVE_NAMESPACE
    marker_present = "encrypted_function_args" in item
    marker = item.get("encrypted_function_args")
    if marker_present and marker != []:
        raise ValueError("wire collaboration response contains encrypted function arguments")
    if item.get("name") not in PLAINTEXT_TOOLS:
        return

    if validate_arguments:
        _validate_plaintext_arguments(item.get("arguments"))
    item["encrypted_function_args"] = []


def _contains_opaque_message(arguments: Any) -> bool:
    if isinstance(arguments, str):
        stripped = arguments.strip()
        if _FERNET_TOKEN_RE.fullmatch(stripped):
            return True
        if not stripped:
            return False
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


__all__ = [
    "NATIVE_NAMESPACE",
    "NATIVE_V2_TOOLS",
    "PLAINTEXT_TOOLS",
    "WIRE_NAMESPACE",
    "prepare_request",
    "transform_response_event",
]
