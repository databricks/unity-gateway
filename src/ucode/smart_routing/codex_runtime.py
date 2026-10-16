"""Codex orchestration request and response policies for the local Responses relay."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

NATIVE_NAMESPACE = "collaboration"
WIRE_NAMESPACE = "ucode_collaboration"
PLAINTEXT_TOOLS = frozenset({"spawn_agent", "send_message", "followup_task"})
_FERNET_TOKEN_RE = re.compile(r"^gAAAAA[A-Za-z0-9_-]+={0,2}$")
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
_MAX_NATIVE_IDENTIFIER_LENGTH = 512
_MAX_SSE_EVENT_BYTES = 16 * 1024 * 1024
_ACTIVATION_SENTENCE = "Smart Router Orchestrator is on for this session."
_ACTIVATION_HEADER = "---\nname: smart-router-orchestrator"
_TRUNCATION_WARNING = re.compile(
    r"^Warning: truncated output \(original token count: [^\n]*\)\n"
    r"Total output lines: \d+\n\n"
)
_OWNER_CONTRACT_BEGIN = "<!-- DELEGATED_OWNER_CONTRACT_BEGIN -->"
_OWNER_CONTRACT_END = "<!-- DELEGATED_OWNER_CONTRACT_END -->"
_OWNER_CAPABILITY = (
    "Controller capability: delegated_owner_contract is delivered directly as a trusted "
    "developer instruction to every child before each ordinary generation. Do not repeat "
    "the marked owner contract in the child brief. Include the task, caller constraints and "
    "the remaining execution contract normally."
)
_WAIT_CAPABILITY = (
    "Controller capability: await_completion is available on native spawn_agent and "
    "followup_task. When one owner has all remaining work and the root has no independent "
    "work, set await_completion=true; after a successful bound assignment, the controller "
    "issues the native maximum-duration completion wait without another model generation. "
    "For an explicit update cadence or deadline that requires a shorter wait, leave it false "
    "and use the normal native wait. The root still reviews coverage, evidence, and quality "
    "after the owner returns."
)
_EXPECTED_RESPONSE_EVENTS = frozenset(
    {
        "response.output_item.added",
        "response.output_item.done",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
        "response.completed",
    }
)


@dataclass
class _ThreadState:
    root_id: str | None
    wait_flags: dict[str, bool] = field(default_factory=dict)
    call_items: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PreparedRequest:
    body: bytes
    synthetic_response: bytes | None = None
    adapt_response: bool = False
    thread_id: str | None = None
    synthetic_content_type: str | None = None
    _thread_state: _ThreadState | None = field(default=None, repr=False, compare=False)
    _wait_enabled: bool = field(default=False, repr=False, compare=False)


class CodexRuntime:
    """Apply the active policy set without changing Codex model or effort fields."""

    max_response_bytes = 16 * 1024 * 1024

    def __init__(self, skill_body: str, enabled: Callable[[], bool]):
        if not isinstance(skill_body, str):
            raise TypeError("Codex runtime skill body must be text")
        if not callable(enabled):
            raise TypeError("Codex runtime enabled control must be callable")
        self._enabled = enabled
        self._skill_sha256 = hashlib.sha256(skill_body.encode("utf-8")).hexdigest()
        self._binding = {"body": skill_body, "sha256": self._skill_sha256}
        self._owner_contract = bound_owner_contract(self._binding)
        self._lock = threading.RLock()
        self._threads: dict[str, _ThreadState] = {}
        self._thread_roots: dict[str, str | None] = {}
        self._thread_parents: dict[str, str | None] = {}
        self._activated_roots: set[str] = set()

    def prepare_request(self, body: bytes, headers: Mapping[str, str]) -> PreparedRequest:
        if not self._enabled():
            return PreparedRequest(body=body)
        if not isinstance(body, bytes):
            raise ValueError("active Codex Responses request body must be bytes")
        payload = _load_json_object(body, "active Codex Responses request")
        if not isinstance(payload.get("input"), list):
            raise ValueError("active Codex Responses request requires a native input list")
        if "stream" in payload and type(payload["stream"]) is not bool:
            raise ValueError("active Codex Responses stream option must be a boolean")
        thread_id, parent_thread_id = _native_thread_identity(headers, payload)
        request_kind = _request_kind(payload)

        with self._lock:
            root_id = self._resolve_root(thread_id, parent_thread_id)
            state = self._thread_state(thread_id, root_id)
            is_root = parent_thread_id is None
            changed = False
            if is_root:
                changed = restore_full_activation(payload, self._binding) or changed
                if _has_bound_activation(payload, self._binding):
                    self._activated_roots.add(thread_id)
            elif root_id in self._activated_roots and self._owner_contract is not None:
                if request_kind != "compaction":
                    changed = inject_owner_contract(payload, self._owner_contract) or changed

            root_activated = is_root and thread_id in self._activated_roots
            wait_enabled = root_activated
            if wait_enabled:
                changed = extend_collaboration_schema(payload) or changed
                wait_binding = eligible_wait(payload, state.wait_flags)
                if wait_binding is not None:
                    response, content_type = _controller_wait(wait_binding, thread_id, payload)
                    return PreparedRequest(
                        body=body,
                        synthetic_response=response,
                        thread_id=thread_id,
                        synthetic_content_type=content_type,
                        _thread_state=state,
                    )
            _prune_wait_state(payload, state)

            if is_root and payload.get("prompt_cache_key") == thread_id:
                model = payload.get("model")
                if not isinstance(model, str) or not model:
                    raise ValueError("default Codex prompt cache key requires a model")
                profile = _cache_profile(payload, model, self._skill_sha256)
                payload["prompt_cache_key"] = hashlib.sha256(
                    json.dumps(
                        profile, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    ).encode("utf-8")
                ).hexdigest()
                changed = True

            forwarded = _encode_json(payload) if changed else body
            forwarded, transport_changed = prepare_transport_request(forwarded)
            adapt_response = (
                transport_changed
                or _has_collaboration_tools(payload)
                or (wait_enabled and _has_delegation_tools(payload))
            )
            return PreparedRequest(
                body=forwarded,
                adapt_response=adapt_response,
                thread_id=thread_id,
                _thread_state=state,
                _wait_enabled=wait_enabled,
            )

    def response_transformer(self, prepared: PreparedRequest) -> _ResponseTransformer:
        if not prepared.adapt_response:
            return _ResponseTransformer(None, None, False)
        if prepared.thread_id is None or prepared._thread_state is None:
            raise ValueError("adapted Codex response has no bound native thread")
        return _ResponseTransformer(self._lock, prepared._thread_state, prepared._wait_enabled)

    def transform_json_response(self, body: bytes, prepared: PreparedRequest) -> bytes:
        if not prepared.adapt_response:
            return body
        if not isinstance(body, bytes):
            raise ValueError("active Codex JSON response must be bytes")
        if len(body) > self.max_response_bytes:
            raise ValueError("Codex JSON response exceeds the bounded limit")
        event = _load_json_object(body, "active Codex JSON response")
        transformed = self.response_transformer(prepared).transform_event(event)
        if transformed == event:
            return body
        return _encode_json(transformed)

    def _resolve_root(self, thread_id: str, parent_thread_id: str | None) -> str | None:
        previous_parent = self._thread_parents.get(thread_id, _UNSET)
        if previous_parent is not _UNSET and previous_parent != parent_thread_id:
            raise ValueError("native Codex thread changed its parent identity")
        if parent_thread_id == thread_id:
            raise ValueError("native Codex thread cannot be its own parent")
        self._thread_parents[thread_id] = parent_thread_id
        if parent_thread_id is None:
            root_id = thread_id
        else:
            root_id = self._thread_roots.get(parent_thread_id)
        previous_root = self._thread_roots.get(thread_id, _UNSET)
        if (
            previous_root is not _UNSET
            and root_id is not None
            and previous_root not in (None, root_id)
        ):
            raise ValueError("native Codex thread was rebound to another root")
        if root_id is None and thread_id in self._thread_roots:
            root_id = self._thread_roots[thread_id]
        self._thread_roots[thread_id] = root_id
        return root_id

    def _thread_state(self, thread_id: str, root_id: str | None) -> _ThreadState:
        state = self._threads.get(thread_id)
        if state is None:
            state = _ThreadState(root_id=root_id)
            self._threads[thread_id] = state
        elif state.root_id is None:
            state.root_id = root_id
        elif root_id is not None and state.root_id != root_id:
            raise ValueError("native Codex thread state belongs to another root")
        return state


_UNSET = object()


def bound_owner_contract(binding: Mapping[str, Any]) -> str | None:
    body = binding.get("body")
    if not isinstance(body, str) or hashlib.sha256(body.encode("utf-8")).hexdigest() != binding.get(
        "sha256"
    ):
        raise ValueError("owner contract requires the complete hash-bound skill")
    if _OWNER_CONTRACT_BEGIN not in body and _OWNER_CONTRACT_END not in body:
        return None
    if (
        body.count(_OWNER_CONTRACT_BEGIN) != 1
        or body.count(_OWNER_CONTRACT_END) != 1
        or body.index(_OWNER_CONTRACT_BEGIN) >= body.index(_OWNER_CONTRACT_END)
    ):
        raise ValueError("owner contract markers must form exactly one ordered pair")
    contract = body.split(_OWNER_CONTRACT_BEGIN, 1)[1].split(_OWNER_CONTRACT_END, 1)[0].strip()
    if not contract:
        raise ValueError("owner contract must not be empty")
    digest = hashlib.sha256(contract.encode("utf-8")).hexdigest()
    return f'<delegated_owner_contract sha256="{digest}">\n{contract}\n</delegated_owner_contract>'


def inject_owner_contract(payload: dict[str, Any], contract: str) -> bool:
    items = payload.get("input")
    if not isinstance(items, list):
        raise ValueError("owner contract requires a native input list")
    existing = []
    for item in items:
        if not isinstance(item, dict) or item.get("role") != "developer":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            text = part.get("text") if isinstance(part, dict) else None
            if isinstance(text, str) and text.startswith("<delegated_owner_contract "):
                existing.append(text)
    if existing:
        if existing != [contract]:
            raise ValueError("stale or duplicate trusted owner contract")
        return False
    items.insert(
        0,
        {
            "type": "message",
            "role": "developer",
            "content": [{"type": "input_text", "text": contract}],
        },
    )
    return True


def restore_full_activation(payload: dict[str, Any], binding: Mapping[str, Any]) -> bool:
    body, expected = binding.get("body"), binding.get("sha256")
    if not isinstance(body, str) or hashlib.sha256(body.encode("utf-8")).hexdigest() != expected:
        return False
    items = payload.get("input")
    if not isinstance(items, list):
        raise ValueError("full skill activation requires a native input list")
    matches = []
    for item in items:
        if not isinstance(item, dict) or item.get("role") != "developer":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or not isinstance(part.get("text"), str):
                continue
            text = _TRUNCATION_WARNING.sub("", part["text"])
            activation = text.find(_ACTIVATION_SENTENCE)
            header = text.find(_ACTIVATION_HEADER, activation + len(_ACTIVATION_SENTENCE))
            if activation < 0 or header < 0:
                continue
            if text.count(_ACTIVATION_SENTENCE) != 1 or text.count(_ACTIVATION_HEADER) != 1:
                raise ValueError("native request contains an ambiguous trusted UG activation")
            matches.append((part, text, header))
    if not matches:
        return False
    if len(matches) != 1:
        raise ValueError("native request contains duplicate trusted UG activations")
    part, text, header = matches[0]
    prefix = text[:header]
    contract = bound_owner_contract(binding)
    additions = []
    if contract is not None:
        additions.append(_OWNER_CAPABILITY)
    additions.append(_WAIT_CAPABILITY)
    replacement = prefix + body + "\n\n" + "\n\n".join(additions)
    if part["text"] == replacement:
        return False
    part["text"] = replacement
    return True


def _has_bound_activation(payload: dict[str, Any], binding: Mapping[str, Any]) -> bool:
    body, expected = binding.get("body"), binding.get("sha256")
    if not isinstance(body, str) or hashlib.sha256(body.encode("utf-8")).hexdigest() != expected:
        return False
    items = payload.get("input")
    if not isinstance(items, list):
        return False
    for item in items:
        if not isinstance(item, dict) or item.get("role") != "developer":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            text = part.get("text") if isinstance(part, dict) else None
            if not isinstance(text, str):
                continue
            normalized = _TRUNCATION_WARNING.sub("", text)
            activation = normalized.find(_ACTIVATION_SENTENCE)
            header = normalized.find(_ACTIVATION_HEADER, activation + len(_ACTIVATION_SENTENCE))
            if activation >= 0 and header >= 0 and body in normalized[header:]:
                return True
    return False


def _load_json_object(body: bytes, label: str) -> dict[str, Any]:
    if not isinstance(body, bytes):
        raise ValueError(f"{label} must be bytes")
    try:
        payload = json.loads(
            body, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} is malformed") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")


def _encode_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _native_thread_identity(
    headers: Mapping[str, str], payload: dict[str, Any]
) -> tuple[str, str | None]:
    if not isinstance(headers, Mapping):
        raise ValueError("active Codex Responses request has no native headers")
    normalized = {}
    for name, value in headers.items():
        if not isinstance(name, str):
            continue
        key = name.lower()
        if key in normalized and normalized[key] != value:
            raise ValueError(f"native Codex header {key!r} is ambiguous")
        normalized[key] = value

    metadata = payload.get("client_metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("native Codex client_metadata must be an object")
    header_thread = _identifier(normalized.get("thread-id"), "thread-id")
    metadata_thread = _identifier(metadata.get("thread_id"), "client_metadata.thread_id")
    if (
        header_thread is not None
        and metadata_thread is not None
        and header_thread != metadata_thread
    ):
        raise ValueError("native Codex thread identity does not match its metadata")
    thread_id = header_thread or metadata_thread
    if thread_id is None:
        raise ValueError("active Codex request requires a stable native thread id")

    header_parent = _identifier(
        normalized.get("x-codex-parent-thread-id"), "x-codex-parent-thread-id"
    )
    metadata_parent = _identifier(
        metadata.get("x-codex-parent-thread-id"), "client_metadata.x-codex-parent-thread-id"
    )
    if (
        header_parent is not None
        and metadata_parent is not None
        and header_parent != metadata_parent
    ):
        raise ValueError("native Codex parent identity does not match its metadata")
    parent_thread_id = header_parent or metadata_parent
    header_subagent = _identifier(normalized.get("x-openai-subagent"), "x-openai-subagent")
    metadata_subagent = _identifier(
        metadata.get("x-openai-subagent"), "client_metadata.x-openai-subagent"
    )
    if (
        header_subagent is not None
        and metadata_subagent is not None
        and header_subagent != metadata_subagent
    ):
        raise ValueError("native Codex subagent identity does not match its metadata")
    subagent = header_subagent or metadata_subagent
    if subagent is not None and parent_thread_id is None:
        raise ValueError("native Codex subagent request has no parent thread id")
    if parent_thread_id == thread_id:
        raise ValueError("native Codex thread cannot be its own parent")
    return thread_id, parent_thread_id


def _identifier(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"native Codex {label} must be text")
    value = value.strip()
    if (
        not value
        or len(value) > _MAX_NATIVE_IDENTIFIER_LENGTH
        or any(character in value for character in "\r\n\x00")
    ):
        raise ValueError(f"native Codex {label} is invalid")
    return value


def _request_kind(payload: dict[str, Any]) -> str | None:
    metadata = payload.get("client_metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("native Codex client_metadata must be an object")
    encoded = metadata.get("x-codex-turn-metadata")
    if encoded is None:
        return None
    if not isinstance(encoded, str):
        raise ValueError("native Codex turn metadata must be text")
    try:
        context = json.loads(
            encoded, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("native Codex turn metadata is malformed") from exc
    if not isinstance(context, dict):
        raise ValueError("native Codex turn metadata must be an object")
    kind = context.get("request_kind")
    if kind is not None and not isinstance(kind, str):
        raise ValueError("native Codex request kind must be text")
    return kind


def _cache_profile(payload: dict[str, Any], model: str, skill_sha256: str) -> dict[str, Any]:
    additional = []
    for item in payload.get("input", []):
        if isinstance(item, dict) and item.get("type") == "additional_tools":
            tools = item.get("tools")
            if not isinstance(tools, list):
                raise ValueError("Responses Lite additional_tools must contain a list")
            additional.append(tools)
    tools = payload.get("tools", [])
    if not isinstance(tools, list):
        raise ValueError("native tools must be a list")
    return {
        "model": model,
        "skill_sha256": skill_sha256,
        "tools": tools,
        "additional_tools": additional,
    }


def _has_collaboration_tools(payload: dict[str, Any]) -> bool:
    for tools in _request_tool_sets(payload):
        for declaration in tools:
            if isinstance(declaration, dict) and (
                declaration.get("name") in COLLABORATION_NAMESPACES
                or declaration.get("namespace") in COLLABORATION_NAMESPACES
            ):
                return True
    return False


def _has_delegation_tools(payload: dict[str, Any]) -> bool:
    for tools in _request_tool_sets(payload):
        for declaration in tools:
            if not isinstance(declaration, dict):
                continue
            nested = (
                declaration.get("tools", [])
                if declaration.get("type") == "namespace"
                else [declaration]
            )
            if not isinstance(nested, list):
                continue
            if any(
                isinstance(tool, dict) and tool.get("name") in DELEGATION_TOOLS for tool in nested
            ):
                return True
    return False


def _request_tool_sets(payload: dict[str, Any]) -> list[list[Any]]:
    sets = []
    if "tools" in payload:
        tools = payload["tools"]
        if not isinstance(tools, list):
            raise ValueError("native tools must be a list")
        sets.append(tools)
    for item in payload.get("input", []):
        if isinstance(item, dict) and item.get("type") == "additional_tools":
            additional = item.get("tools")
            if not isinstance(additional, list):
                raise ValueError("Responses Lite additional_tools must contain a list")
            sets.append(additional)
    return sets


def _prune_wait_state(payload: dict[str, Any], state: _ThreadState) -> None:
    items = payload.get("input", [])
    call_ids = {
        item.get("call_id")
        for item in items
        if isinstance(item, dict)
        and item.get("type") == "function_call"
        and isinstance(item.get("call_id"), str)
    }
    output_ids = {
        item.get("call_id")
        for item in items
        if isinstance(item, dict)
        and item.get("type") == "function_call_output"
        and isinstance(item.get("call_id"), str)
    }
    for call_id in tuple(state.call_items):
        if call_id not in call_ids or call_id in output_ids:
            state.call_items.pop(call_id, None)
            state.wait_flags.pop(call_id, None)


def _controller_wait(
    binding: dict[str, Any], thread_id: str, payload: dict[str, Any]
) -> tuple[bytes, str]:
    source_call_id = binding["source_call_id"]
    action_id = hashlib.sha256(
        json.dumps({"thread": thread_id, "source": source_call_id}, sort_keys=True).encode("utf-8")
    ).hexdigest()[:32]
    item = {
        "id": "fc_controller_" + action_id,
        "type": "function_call",
        "call_id": "call_controller_" + action_id,
        "name": "wait_agent",
        "namespace": "collaboration",
        "arguments": '{"timeout_ms":3600000}',
        "status": "completed",
    }
    response = {
        "id": "resp_controller_wait_" + action_id,
        "object": "response",
        "status": "completed",
        "output": [item],
        "metadata": {"origin": "controller", "action": "native_completion_wait"},
    }
    if payload.get("stream", False) is True:
        events = [
            ("response.created", {"response": {**response, "status": "in_progress", "output": []}}),
            (
                "response.output_item.added",
                {"output_index": 0, "item": {**item, "arguments": "", "status": "in_progress"}},
            ),
            (
                "response.function_call_arguments.delta",
                {"output_index": 0, "item_id": item["id"], "delta": item["arguments"]},
            ),
            (
                "response.function_call_arguments.done",
                {"output_index": 0, "item_id": item["id"], "arguments": item["arguments"]},
            ),
            ("response.output_item.done", {"output_index": 0, "item": item}),
            ("response.completed", {"response": response}),
        ]
        body = b"".join(
            (
                "event: "
                + kind
                + "\ndata: "
                + json.dumps(
                    {"type": kind, **value, "sequence_number": index},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n\n"
            ).encode("utf-8")
            for index, (kind, value) in enumerate(events)
        )
        return body, "text/event-stream"
    return _encode_json(response), "application/json"


class _ResponseTransformer:
    def __init__(
        self,
        lock: threading.RLock | None,
        state: _ThreadState | None,
        wait_enabled: bool,
    ):
        self._lock = lock
        self._state = state
        self._wait_enabled = wait_enabled
        self._buffer = bytearray()
        self._function_items: dict[str, tuple[str | None, str | None, str | None]] = {}
        self._delta_arguments: dict[str, list[str]] = {}
        self._finalized: dict[str, tuple[str, str, bool | None]] = {}

    def feed(self, chunk: bytes) -> list[bytes]:
        if not isinstance(chunk, bytes):
            raise ValueError("Codex Responses SSE chunk must be bytes")
        if self._lock is None:
            return [chunk] if chunk else []
        self._buffer.extend(chunk)
        result = []
        while separator := _sse_separator(self._buffer):
            index, separator_length = separator
            frame_size = index + separator_length
            if frame_size > _MAX_SSE_EVENT_BYTES:
                raise ValueError("Codex Responses SSE event exceeds the bounded limit")
            frame = bytes(self._buffer[:frame_size])
            del self._buffer[:frame_size]
            transformed = self._transform_frame(frame)
            if transformed:
                result.append(transformed)
        if len(self._buffer) > _MAX_SSE_EVENT_BYTES:
            raise ValueError("Codex Responses SSE event exceeds the bounded limit")
        return result

    def finish(self) -> list[bytes]:
        if not self._buffer:
            return []
        if self._buffer.strip(b"\r\n :"):
            raise ValueError("Codex Responses SSE stream ended mid-event")
        trailing = bytes(self._buffer)
        self._buffer.clear()
        return [trailing] if trailing else []

    def transform_event(self, event: dict[str, Any]) -> dict[str, Any]:
        if self._lock is None:
            return event
        result = transform_transport_event(event)
        if not self._wait_enabled:
            return result
        item = result.get("item")
        if isinstance(item, dict):
            self._transform_item(item, final=result.get("type") != "response.output_item.added")
        for response in (result, result.get("response")):
            if not isinstance(response, dict):
                continue
            output = response.get("output")
            if isinstance(output, list):
                for output_item in output:
                    if isinstance(output_item, dict):
                        self._transform_item(output_item, final=True)
        self._transform_argument_boundary(result)
        return result

    def _transform_frame(self, frame: bytes) -> bytes:
        lines = frame.splitlines(keepends=True)
        data_indices = []
        data_values = []
        event_name = None
        for index, line in enumerate(lines):
            content = _line_content(line)
            if content.startswith(b"event:"):
                event_name = content[6:].strip().decode("ascii", errors="replace")
            if content.startswith(b"data:"):
                value = content[5:]
                if value.startswith(b" "):
                    value = value[1:]
                data_indices.append(index)
                data_values.append(value)
        if not data_indices:
            return frame
        data = b"\n".join(data_values)
        if data == b"[DONE]":
            return frame
        try:
            event = _load_json_object(data, "Codex Responses SSE event")
        except ValueError:
            if event_name in _EXPECTED_RESPONSE_EVENTS:
                raise
            return frame
        transformed = self.transform_event(event)
        if transformed == event:
            return frame
        encoded = json.dumps(transformed, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        first_data = data_indices[0]
        output = []
        for index, line in enumerate(lines):
            if index == first_data:
                output.append(b"data: " + encoded + _line_ending(line))
            elif index not in data_indices:
                output.append(line)
        return b"".join(output)

    def _transform_argument_boundary(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type not in {
            "response.function_call_arguments.delta",
            "response.function_call_arguments.done",
        }:
            return
        item_id = event.get("item_id")
        if not isinstance(item_id, str):
            raise ValueError("Codex function-argument event has no item id")
        binding = self._function_items.get(item_id)
        if binding is None:
            raise ValueError("Codex function-argument event has no preceding item binding")
        namespace, name, call_id = binding
        if namespace != NATIVE_NAMESPACE or name not in DELEGATION_TOOLS:
            return
        if not isinstance(call_id, str) or not call_id:
            raise ValueError("Codex delegation item has no call id")
        if event_type == "response.function_call_arguments.delta":
            delta = event.get("delta")
            if not isinstance(delta, str):
                raise ValueError("Codex delegation argument delta must be text")
            self._delta_arguments.setdefault(item_id, []).append(delta)
            event["delta"] = ""
        else:
            arguments = event.get("arguments")
            if not isinstance(arguments, str):
                raise ValueError("Codex delegation arguments must be text")
            fragments = self._delta_arguments.get(item_id)
            if fragments is not None and "".join(fragments) != arguments:
                raise ValueError("Codex delegation argument stream is truncated or inconsistent")
            event["arguments"], hint = _strip_wait_hint(arguments)
            self._record_final(item_id, call_id, event["arguments"], hint)

    def _transform_item(self, item: dict[str, Any], *, final: bool) -> None:
        if item.get("type") != "function_call":
            return
        item_id, namespace, name, call_id = (
            item.get("id"),
            item.get("namespace"),
            item.get("name"),
            item.get("call_id"),
        )
        if isinstance(item_id, str):
            binding = (
                namespace if isinstance(namespace, str) else None,
                name if isinstance(name, str) else None,
                call_id if isinstance(call_id, str) else None,
            )
            previous = self._function_items.get(item_id)
            if previous is not None and previous != binding:
                raise ValueError("Codex response item changed its native call binding")
            self._function_items[item_id] = binding
        if namespace != NATIVE_NAMESPACE or name not in DELEGATION_TOOLS:
            return
        if (
            not isinstance(item_id, str)
            or not item_id
            or not isinstance(call_id, str)
            or not call_id
        ):
            raise ValueError("Codex delegation item requires native item and call ids")
        if not self._wait_enabled:
            return
        arguments = item.get("arguments")
        if not isinstance(arguments, str):
            if final:
                raise ValueError("Codex delegation arguments must be text")
            return
        if not final:
            item["arguments"] = ""
            return
        cleaned, hint = _strip_wait_hint(arguments)
        if cleaned != arguments:
            item["arguments"] = cleaned
        if final:
            self._record_final(item_id, call_id, cleaned, hint)

    def _record_final(self, item_id: str, call_id: str, arguments: str, hint: bool | None) -> None:
        signature = (call_id, arguments, hint)
        previous = self._finalized.get(item_id)
        if previous is not None:
            if previous != signature:
                raise ValueError("Codex delegation item has conflicting final arguments")
            return
        self._finalized[item_id] = signature
        assert self._lock is not None and self._state is not None
        with self._lock:
            previous_item = self._state.call_items.get(call_id)
            if previous_item is not None and previous_item != item_id:
                self._state.wait_flags.pop(call_id, None)
                raise ValueError("Codex delegation reused a call id for another response item")
            self._state.call_items[call_id] = item_id
            if hint is True:
                self._state.wait_flags[call_id] = True
            else:
                self._state.wait_flags.pop(call_id, None)


def _strip_wait_hint(arguments: str) -> tuple[str, bool | None]:
    try:
        parsed = json.loads(
            arguments, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("Codex delegation arguments are malformed") from exc
    if not isinstance(parsed, dict) or "await_completion" not in parsed:
        return arguments, None
    hint = parsed.pop("await_completion")
    if type(hint) is not bool:
        raise ValueError("Codex await_completion hint must be a boolean")
    return _encode_json(parsed).decode("utf-8"), hint


def _sse_separator(buffer: bytearray) -> tuple[int, int] | None:
    matches = [
        (index, len(separator))
        for separator in (b"\r\n\r\n", b"\n\n")
        if (index := buffer.find(separator)) >= 0
    ]
    return min(matches) if matches else None


def _line_content(line: bytes) -> bytes:
    if line.endswith(b"\r\n"):
        return line[:-2]
    if line.endswith((b"\n", b"\r")):
        return line[:-1]
    return line


def _line_ending(line: bytes) -> bytes:
    if line.endswith(b"\r\n"):
        return b"\r\n"
    if line.endswith(b"\n"):
        return b"\n"
    if line.endswith(b"\r"):
        return b"\r"
    return b""


def prepare_transport_request(body: bytes) -> tuple[bytes, bool]:
    """Rewrite native tool schemas and plaintext history, preserving encrypted replay."""
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


def transform_transport_event(event: dict[str, Any]) -> dict[str, Any]:
    """Restore native calls and reject opaque plaintext assignments before dispatch."""
    if not isinstance(event, dict):
        raise ValueError("Codex v2 response event must be a JSON object")

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
        changed = True
        functions = tool.get("tools")
        if isinstance(functions, list):
            for function in functions:
                if isinstance(function, dict) and function.get("name") in PLAINTEXT_TOOLS:
                    _remove_message_encrypted_marker(function)
    return changed


def _remove_message_encrypted_marker(tool: dict[str, Any]) -> None:
    parameters = tool.get("parameters")
    properties = parameters.get("properties") if isinstance(parameters, dict) else None
    message = properties.get("message") if isinstance(properties, dict) else None
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
        return False
    item["namespace"] = WIRE_NAMESPACE
    if item.get("type") == "function_call" and item.get("name") in PLAINTEXT_TOOLS:
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
