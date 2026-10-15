"""Codex's optional, controller-bound delegated completion wait."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

COLLABORATION_NAMESPACES = frozenset({"collaboration", "ucode_collaboration"})
DELEGATION_TOOLS = frozenset({"spawn_agent", "followup_task"})
_AWAIT_COMPLETION = {
    "type": "boolean",
    "description": (
        "Set true only when this owner has all remaining work, no independent parent work, "
        "and no explicit cadence/deadline requiring shorter native waits"
    ),
}
_TASK_NAME = re.compile(r"(?:/root/|root/)?[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*\Z")
_DOCUMENTED_MAX = re.compile(r"\bmax(?:imum)?\s*(?::|=)?\s*([\d,]+)\b", re.IGNORECASE)


def extend_collaboration_schema(payload: dict[str, Any]) -> bool:
    """Add an optional wait hint to native collaboration delegation tools."""
    changed = False
    for tools in _tool_sets(payload):
        for declaration in tools:
            if not isinstance(declaration, dict):
                continue
            declaration_type = declaration.get("type")
            if (
                declaration_type == "namespace"
                and declaration.get("name") in COLLABORATION_NAMESPACES
            ):
                namespace = declaration["name"]
                namespace_tools = declaration.get("tools", [])
            elif (
                declaration_type in {"function", "custom"}
                and declaration.get("namespace") in COLLABORATION_NAMESPACES
            ):
                namespace = declaration["namespace"]
                namespace_tools = [declaration]
            else:
                continue
            if not isinstance(namespace_tools, list):
                raise ValueError(f"{namespace} tool namespace must contain a list")
            for tool in namespace_tools:
                if (
                    not isinstance(tool, dict)
                    or tool.get("type") not in {"function", "custom"}
                    or tool.get("name") not in DELEGATION_TOOLS
                ):
                    continue
                parameters = tool.get("parameters")
                properties = parameters.get("properties") if isinstance(parameters, dict) else None
                if not isinstance(properties, dict):
                    raise ValueError("collaboration delegation tool has no parameter properties")
                if (
                    "await_completion" in properties
                    and properties["await_completion"] != _AWAIT_COMPLETION
                ):
                    raise ValueError("unsupported existing await_completion schema")
                if "await_completion" not in properties:
                    properties["await_completion"] = dict(_AWAIT_COMPLETION)
                    changed = True
                required = parameters.get("required")
                if required is not None and not isinstance(required, list):
                    raise ValueError("collaboration tool required fields must be a list")
                if isinstance(required, list) and "await_completion" in required:
                    parameters["required"] = [
                        name for name in required if name != "await_completion"
                    ]
                    changed = True
    return changed


def eligible_wait(
    payload: dict[str, Any], declared_calls: Mapping[str, bool] | None = None
) -> dict[str, Any] | None:
    """Return a synthetic wait binding only for a declared, successful sole owner."""
    items = payload.get("input")
    if not isinstance(items, list) or not items:
        return None
    final_output = items[-1]
    if not isinstance(final_output, dict) or final_output.get("type") != "function_call_output":
        return None
    source_call_id = final_output.get("call_id")
    if not isinstance(source_call_id, str) or not source_call_id:
        return None

    wait_bindings = []
    for tools in _tool_sets(payload):
        for declaration in tools:
            binding = _namespace_tools(declaration)
            if binding is None:
                continue
            namespace, namespace_tools = binding
            for tool in namespace_tools:
                if (
                    isinstance(tool, dict)
                    and tool.get("type") in {"function", "custom"}
                    and tool.get("name") == "wait_agent"
                ):
                    wait_bindings.append((namespace, tool))
    if len(wait_bindings) != 1:
        return None
    wait_namespace, wait_tool = wait_bindings[0]
    parameters = wait_tool.get("parameters")
    properties = parameters.get("properties") if isinstance(parameters, dict) else None
    timeout = properties.get("timeout_ms") if isinstance(properties, dict) else None
    if (
        not isinstance(timeout, dict)
        or timeout.get("type") not in {"number", "integer"}
        or isinstance(timeout.get("maximum"), bool)
    ):
        return None
    maximum = timeout.get("maximum")
    description = timeout.get("description", "")
    if not isinstance(description, str):
        return None
    match = _DOCUMENTED_MAX.search(description)
    documented_max = int(match.group(1).replace(",", "")) if match else None
    if maximum is not None and maximum != 3_600_000:
        return None
    if documented_max is not None and documented_max != 3_600_000:
        return None
    if maximum != 3_600_000 and documented_max != 3_600_000:
        return None

    source_calls = [
        (index, item)
        for index, item in enumerate(items[:-1])
        if isinstance(item, dict)
        and item.get("type") == "function_call"
        and item.get("call_id") == source_call_id
    ]
    if len(source_calls) != 1:
        return None
    source_index, source_call = source_calls[0]
    if any(
        isinstance(event, dict)
        and (
            event.get("type") == "agent_message"
            or (
                event.get("type") == "message"
                and event.get("role") in {"user", "developer", "agent"}
            )
        )
        for event in items[source_index + 1 : -1]
    ):
        return None

    spawn_bindings: dict[str, str] = {}
    owners = set()
    for index, item in enumerate(items[:-1]):
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        name, arguments = _call_parts(item)
        if name == "followup_task" and arguments is not None:
            targets = [
                _normalize_task_name(arguments[key])
                for key in ("target", "task_name")
                if key in arguments
            ]
            if not targets or None in targets or len(set(targets)) != 1:
                return None
            owners.update(targets)
            continue
        if name != "spawn_agent" or arguments is None:
            continue
        call_id = item.get("call_id")
        requested_owner = _normalize_task_name(arguments.get("task_name"))
        outputs = [
            event
            for event in items[index + 1 :]
            if isinstance(event, dict)
            and event.get("type") == "function_call_output"
            and event.get("call_id") == call_id
        ]
        if (
            not isinstance(call_id, str)
            or not call_id
            or requested_owner is None
            or len(outputs) != 1
        ):
            return None
        try:
            result = json.loads(outputs[0].get("output", ""))
        except (TypeError, ValueError):
            return None
        returned_owner = (
            _normalize_task_name(result.get("task_name")) if isinstance(result, dict) else None
        )
        if (
            returned_owner is None
            or returned_owner != requested_owner
            or result.get("error")
            or result.get("ok") is False
            or result.get("success") is False
        ):
            return None
        spawn_bindings[call_id] = returned_owner
        owners.add(returned_owner)
    if len(owners) != 1:
        return None
    owner = next(iter(owners))

    source_name, source_arguments = _call_parts(source_call)
    if (
        source_name is None
        or source_arguments is None
        or declared_calls is None
        or declared_calls.get(source_call_id) is not True
    ):
        return None
    if source_name == "spawn_agent":
        if spawn_bindings.get(source_call_id) != owner:
            return None
    else:
        targets = [
            source_arguments[key] for key in ("target", "task_name") if key in source_arguments
        ]
        if not targets or any(_normalize_task_name(target) != owner for target in targets):
            return None
        output = final_output.get("output")
        if not isinstance(output, str):
            return None
        if output.strip():
            try:
                result = json.loads(output)
            except (TypeError, ValueError):
                return None
            if (
                not isinstance(result, dict)
                or _normalize_task_name(result.get("task_name")) != owner
                or result.get("error")
                or result.get("ok") is False
                or result.get("success") is False
            ):
                return None

    return {
        "owner": owner,
        "source_call_id": source_call_id,
        "verified_tool_binding": {"namespace": wait_namespace, "tool": wait_tool},
    }


def _tool_sets(payload: dict[str, Any]) -> list[list[Any]]:
    result = []
    if "tools" in payload:
        tools = payload["tools"]
        if not isinstance(tools, list):
            raise ValueError("native tools must be a list")
        result.append(tools)
    items = payload.get("input")
    if items is not None and not isinstance(items, list):
        raise ValueError("native input must be a list")
    for item in items or []:
        if isinstance(item, dict) and item.get("type") == "additional_tools":
            tools = item.get("tools")
            if not isinstance(tools, list):
                raise ValueError("Responses Lite additional_tools must contain a list")
            result.append(tools)
    return result


def _namespace_tools(declaration: Any) -> tuple[str, list[Any]] | None:
    if not isinstance(declaration, dict):
        return None
    if (
        declaration.get("type") == "namespace"
        and declaration.get("name") in COLLABORATION_NAMESPACES
    ):
        return declaration["name"], declaration.get("tools", [])
    if (
        declaration.get("type") in {"function", "custom"}
        and declaration.get("namespace") in COLLABORATION_NAMESPACES
    ):
        return declaration["namespace"], [declaration]
    return None


def _call_parts(call: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    namespace = call.get("namespace")
    name = call.get("name")
    if isinstance(name, str) and "." in name:
        prefix, name = name.rsplit(".", 1)
        if namespace is not None and namespace != prefix:
            return None, None
        namespace = prefix
    if namespace not in COLLABORATION_NAMESPACES or name not in DELEGATION_TOOLS:
        return None, None
    try:
        arguments = json.loads(call.get("arguments", ""))
    except (TypeError, ValueError):
        return None, None
    return (name, arguments) if isinstance(arguments, dict) else (None, None)


def _normalize_task_name(value: Any) -> str | None:
    if not isinstance(value, str) or not _TASK_NAME.fullmatch(value):
        return None
    if value.startswith("/root/"):
        value = value[len("/root/") :]
    elif value.startswith("root/"):
        value = value[len("root/") :]
    return "/root/" + value
