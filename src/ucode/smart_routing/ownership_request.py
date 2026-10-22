"""Project a captured Anthropic request without reading files or private thinking."""

from __future__ import annotations

import json

_REQUEST_FIELDS = {
    "model",
    "system",
    "messages",
    "tools",
    "tool_choice",
    "output_config",
    "metadata",
    "max_tokens",
    "thinking",
    "stream",
    "stop_sequences",
    "temperature",
    "top_p",
    "top_k",
    "cache_control",
    "betas",
    "service_tier",
    "speed",
    "diagnostics",
    "context_management",
    # Gateway's SmartRouterRecipeParser consumes this root field before inference.
    "smart_router_recipe_name",
}
_PRIVATE_BLOCKS = {"thinking", "redacted_thinking"}


def project_request(body: dict) -> dict:
    """Return context, context_complete and attachment coverage for one HTTP body.

    This projects the body actually sent, including its current user message. The
    caller supplies task.prompt separately from its authenticated native intent.
    Completeness describes readable, self-contained request content, not whether
    a transcript contains every historical turn or the request belongs to root.
    No content is truncated. Unsupported semantics retain readable siblings and
    report unavailable attachments named by their JSON location, never by a path
    that this function would attempt to open.
    """
    context: list[dict] = []
    attachments: list[dict] = []

    def missing(path: str) -> None:
        attachments.append({"name": path, "available": False})

    def text(role: str, content: str) -> None:
        context.append({"role": role, "content": content})

    def structured(role: str, value: object, path: str) -> None:
        try:
            rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError):
            missing(path)
        else:
            text(role, rendered)

    def extra_fields(value: dict, allowed: set[str], path: str) -> None:
        for key in value:
            if key not in allowed:
                missing(f"{path}.{key}")

    def content_blocks(value: object, role: str, path: str) -> None:
        if isinstance(value, str):
            text(role, value)
            return
        if not isinstance(value, list):
            missing(path)
            return
        for index, block in enumerate(value):
            location = f"{path}[{index}]"
            if not isinstance(block, dict):
                missing(location)
                continue
            kind = block.get("type")
            if isinstance(kind, str) and kind in _PRIVATE_BLOCKS:
                # These contain provider-private reasoning, not caller evidence.
                continue
            if kind == "text":
                extra_fields(block, {"type", "text", "cache_control", "signature"}, location)
                if isinstance(block.get("text"), str):
                    text(role, block["text"])
                else:
                    missing(location)
            elif kind == "tool_use" and role == "assistant":
                extra_fields(
                    block, {"type", "id", "name", "input", "cache_control", "signature"}, location
                )
                if (
                    not isinstance(block.get("id"), str)
                    or not isinstance(block.get("name"), str)
                    or not isinstance(block.get("input"), dict)
                ):
                    missing(location)
                else:
                    structured(
                        role, {key: block[key] for key in ("type", "id", "name", "input")}, location
                    )
            elif kind == "tool_result" and role == "user":
                extra_fields(
                    block,
                    {"type", "tool_use_id", "content", "is_error", "cache_control", "signature"},
                    location,
                )
                if not isinstance(block.get("tool_use_id"), str) or (
                    "is_error" in block and type(block["is_error"]) is not bool
                ):
                    missing(location)
                structured(
                    "tool",
                    {
                        key: block[key]
                        for key in ("type", "tool_use_id", "is_error")
                        if key in block
                    },
                    location,
                )
                content_blocks(block.get("content", []), "tool", f"{location}.content")
            else:
                # Images, documents, references, and unknown future block types
                # cannot be declared readable merely because they have metadata.
                missing(location)

    if not isinstance(body, dict):
        missing("request")
        return {"context": context, "context_complete": False, "attachments": attachments}

    extra_fields(body, _REQUEST_FIELDS, "request")
    if "system" in body:
        content_blocks(body["system"], "system", "system")

    tools = body.get("tools", [])
    if not isinstance(tools, list):
        missing("tools")
    else:
        for index, tool in enumerate(tools):
            location = f"tools[{index}]"
            if (
                not isinstance(tool, dict)
                or not isinstance(tool.get("name"), str)
                or not isinstance(tool.get("input_schema"), dict)
                or ("description" in tool and not isinstance(tool["description"], str))
                or tool.get("type", "custom") != "custom"
            ):
                missing(location)
                continue
            # Serialize the full caller schema, including descriptions, examples
            # and fields literally named "thinking" or "signature". Those are
            # caller data, unlike provider-private content-block metadata.
            structured(
                "system",
                {"tool_definition": {k: v for k, v in tool.items() if k != "cache_control"}},
                location,
            )

    for field in ("tool_choice", "output_config"):
        if field in body:
            if isinstance(body[field], dict):
                structured("system", {field: body[field]}, field)
            else:
                missing(field)

    management = body.get("context_management")
    if management is not None:
        # The pinned host clears private thinking by default. Other edits may
        # change evidence at the provider and need a separately supported view.
        if (
            not isinstance(management, dict)
            or set(management) != {"edits"}
            or not isinstance(management["edits"], list)
            or any(
                not isinstance(edit, dict)
                or edit.get("type") != "clear_thinking_20251015"
                or set(edit) - {"type", "keep"}
                for edit in management["edits"]
            )
        ):
            missing("context_management")

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        missing("messages")
    else:
        for index, message in enumerate(messages):
            location = f"messages[{index}]"
            if not isinstance(message, dict) or message.get("role") not in (
                "user",
                "assistant",
                "system",
            ):
                missing(location)
                continue
            extra_fields(message, {"role", "content", "output_config"}, location)
            content_blocks(message.get("content"), message["role"], f"{location}.content")
            # Native Claude may attach system context and output settings to a
            # message. Both are caller-visible inputs to the ownership decision.
            if "output_config" in message:
                if isinstance(message["output_config"], dict):
                    structured(
                        "system",
                        {"output_config": message["output_config"]},
                        f"{location}.output_config",
                    )
                else:
                    missing(f"{location}.output_config")

    return {"context": context, "context_complete": not attachments, "attachments": attachments}
