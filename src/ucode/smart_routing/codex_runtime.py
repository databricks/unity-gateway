"""Frozen V10 Codex request and response policies for the local Responses relay."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ucode.smart_routing import codex_v2_transport, delegated_wait

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
    """Apply the active V10 policy set without changing Codex model or effort fields."""

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
                changed = delegated_wait.extend_collaboration_schema(payload) or changed
                wait_binding = delegated_wait.eligible_wait(payload, state.wait_flags)
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
            forwarded, transport_changed = codex_v2_transport.prepare_request(forwarded)
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
                declaration.get("name") in delegated_wait.COLLABORATION_NAMESPACES
                or declaration.get("namespace") in delegated_wait.COLLABORATION_NAMESPACES
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
                isinstance(tool, dict) and tool.get("name") in delegated_wait.DELEGATION_TOOLS
                for tool in nested
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
        result = codex_v2_transport.transform_response_event(event)
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
        if (
            namespace != codex_v2_transport.NATIVE_NAMESPACE
            or name not in delegated_wait.DELEGATION_TOOLS
        ):
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
        if (
            namespace != codex_v2_transport.NATIVE_NAMESPACE
            or name not in delegated_wait.DELEGATION_TOOLS
        ):
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
