"""Bind native ownership intents to the assembled main-agent HTTP request."""

from __future__ import annotations

import copy
import hashlib
import json
import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ucode.config_io import atomic_write_json
from ucode.os_compatibility.file_lock_cross_os import (
    acquire_exclusive_file_lock,
    release_file_lock,
)
from ucode.smart_routing import ownership
from ucode.smart_routing.ownership_request import project_request
from ucode.smart_routing.session_env import effective_environment


def _intent_path(session_id: str, request_id: str, env: Mapping[str, str] | None) -> Path:
    state_path, _ = ownership._state_paths(session_id, env)
    key = hashlib.sha256(request_id.encode()).hexdigest()
    return state_path.parent / "intents" / state_path.stem / f"{key}.json"


def _read_intent(path: Path, session_id: str, request_id: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ownership.OwnershipStateError("native ownership intent is unavailable") from exc
    if (
        not isinstance(value, dict)
        or set(value)
        - {
            "schema_version",
            "session_id",
            "request",
            "marker",
            "native_prompt_id",
            "decision",
            "instructions",
            "captured_input_sha256",
        }
        or value.get("schema_version") != 1
        or value.get("session_id") != session_id
        or not isinstance(value.get("request"), dict)
        or value["request"].get("request_id") != request_id
        or not isinstance(value.get("marker"), str)
        or not value["marker"]
        or not {"native_prompt_id", "decision", "instructions"} <= value.keys()
    ):
        raise ownership.OwnershipStateError("native ownership intent identity differs")
    request = ownership.validate_request(value["request"])
    if value["native_prompt_id"] is not None and (
        not isinstance(value["native_prompt_id"], str)
        or not value["native_prompt_id"]
        or value.get("captured_input_sha256") != request["input_sha256"]
    ):
        raise ownership.OwnershipStateError("captured ownership intent identity differs")
    if value["decision"] is not None:
        decision = ownership.validate_decision(
            value["decision"], request, state={"owners": request["existing_owners"]}
        )
        expected = ownership.instructions_for_decision(
            decision, answer_delivery=request["constraints"]["answer_delivery"]
        )
        if value["native_prompt_id"] is None or value["instructions"] != expected:
            raise ownership.OwnershipStateError("captured ownership instructions differ")
    return value


def prepare_request(
    request: object, session_id: str, *, env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Store runtime facts; do not classify a hook's partial transcript."""
    value = ownership.validate_request(request)
    if not value["constraints"]["root_session"] or value["constraints"]["externally_assigned"]:
        raise ownership.OwnershipProtocolError("only a root prompt may prepare ownership")
    path = _intent_path(session_id, value["request_id"], env)
    path.parent.mkdir(parents=True, exist_ok=True)
    marker = f"<ucode-ownership-intent>{secrets.token_hex(24)}</ucode-ownership-intent>"
    intent = {
        "schema_version": 1,
        "session_id": session_id,
        "request": value,
        "marker": marker,
        "native_prompt_id": None,
        "decision": None,
        "instructions": None,
    }
    # A native request identity is single-use, even if two submissions have equal text.
    with path.open("x", encoding="utf-8") as stream:
        json.dump(intent, stream, ensure_ascii=False, allow_nan=False)
    return {
        "request_id": value["request_id"],
        "instructions": marker,
        "policy_sha256": ownership.policy_sha256(),
    }


def pending_instructions(
    session_id: str, request_id: str, *, env: Mapping[str, str] | None = None
) -> str:
    intent = _read_intent(_intent_path(session_id, request_id, env), session_id, request_id)
    return intent["instructions"] or intent["marker"]


def _replace_marker(body: dict, marker: str, replacement: str) -> tuple[dict, int]:
    """Replace only model-visible text blocks, never tool arguments or private thinking."""
    result = copy.deepcopy(body)
    count = 0

    def replace(value: object) -> object:
        nonlocal count
        if isinstance(value, str):
            count += value.count(marker)
            return value.replace(marker, replacement)
        if isinstance(value, list):
            for block in value:
                if isinstance(block, dict) and block.get("type") == "text":
                    block["text"] = replace(block.get("text"))
        return value

    if "system" in result:
        result["system"] = replace(result["system"])
    for message in result.get("messages", []):
        if isinstance(message, dict) and "content" in message:
            message["content"] = replace(message["content"])
    return result, count


def _install_decision(
    session_id: str,
    request: Mapping[str, Any],
    decision: Mapping[str, Any],
    env: Mapping[str, str] | None,
) -> None:
    path, lock_path = ownership._state_paths(session_id, env)
    with lock_path.open("a+b") as stream:
        acquire_exclusive_file_lock(stream)
        try:
            state = ownership._read_state_unlocked(path, session_id)
            current = state.get("current") if state else None
            if not isinstance(current, dict) or current.get("request_id") != request["request_id"]:
                raise ownership.OwnershipStateError("ownership request changed before dispatch")
            assert state is not None
            if current["dispatch_status"] != "undecided":
                if current["input_sha256"] != decision["input_sha256"]:
                    raise ownership.OwnershipStateError(
                        "ownership decision conflicts with current task"
                    )
                return
            ownership.validate_decision(decision, request, state=state)
            current.update(
                {
                    key: decision[key]
                    for key in (
                        "input_sha256",
                        "policy_version",
                        "policy_sha256",
                        "decision_status",
                        "ownership",
                        "dispatch",
                        "owner_agent_id",
                    )
                }
            )
            current["dispatch_status"] = (
                "started" if decision["dispatch"] == "work_directly" else "awaiting_dispatch"
            )
            state["generation"] += 1
            ownership._validate_state(state, session_id)
            atomic_write_json(path, state)
        finally:
            release_file_lock(stream)


def _bind_continuation(
    session_id: str, request_id: str, prompt_id: str, env: Mapping[str, str] | None
) -> None:
    """Consume the permit written by an authenticated native task-notification event."""
    path, lock_path = ownership._state_paths(session_id, env)
    with lock_path.open("a+b") as stream:
        acquire_exclusive_file_lock(stream)
        try:
            state = ownership._read_state_unlocked(path, session_id)
            current = state.get("current") if state else None
            if (
                not isinstance(current, dict)
                or current.get("request_id") != request_id
                or current["dispatch_status"] == "undecided"
                or not current.get("continuation_turn_id")
                or current["continuation_turn_id"] == current.get("turn_id")
                or current.get("continuation_prompt_id") not in (None, prompt_id)
            ):
                raise ownership.OwnershipStateError("a foreign prompt cannot consume ownership")
            assert state is not None
            if current.get("continuation_prompt_id") is None:
                current["continuation_prompt_id"] = prompt_id
                state["generation"] += 1
                ownership._validate_state(state, session_id)
                atomic_write_json(path, state)
        finally:
            release_file_lock(stream)


def _inject_instructions(body: dict, marker: str, instructions: str) -> dict:
    result, replaced = _replace_marker(body, marker, instructions)
    if replaced or _replace_marker(result, instructions, instructions)[1]:
        return result
    # Native compaction can remove the original marker from the next request.
    system = result.get("system", [])
    if isinstance(system, str):
        result["system"] = system + "\n\n" + instructions
    elif isinstance(system, list):
        result["system"] = [*system, {"type": "text", "text": instructions}]
    else:
        raise ownership.OwnershipProtocolError("cannot restore ownership instructions to system")
    return result


def capture_request(
    body: dict,
    headers: Mapping[str, str],
    *,
    workspace: str,
    env: Mapping[str, str] | None = None,
    token: str | None = None,
) -> dict:
    """Resolve a prepared intent before forwarding the first real parent request."""
    effective = effective_environment(env, agent="claude")
    if effective.get("ENABLE_SMART_ROUTER_ORCHESTRATOR") != "1" or not any(
        effective.get(key) == "1"
        for key in ("ENABLE_SMART_ROUTING_V2", "ENABLE_SMART_ROUTING_SUBAGENT_ONLY")
    ):
        return body
    identity = {key.lower(): value for key, value in headers.items()}
    request_class = identity.get("x-claude-code-request-class")
    if request_class in {"subagent", "compaction", "auxiliary"}:
        return body
    session_id = identity.get("x-claude-code-session-id")
    prompt_id = identity.get("x-claude-code-prompt-id")
    if (
        request_class != "main"
        or not session_id
        or not prompt_id
        or identity.get("x-claude-code-agent-id")
    ):
        raise ownership.OwnershipProtocolError(
            "native ownership request headers are missing or inconsistent"
        )
    state = ownership.read_state(session_id, env=env)
    current = state.get("current") if state else None
    if not isinstance(current, dict):
        raise ownership.OwnershipStateError("main request has no prepared ownership intent")
    assert state is not None
    request_id = current["request_id"]
    path = _intent_path(session_id, request_id, env)
    # Single-flight one intent across HTTP retries; child state updates use a separate lock.
    with path.with_suffix(".lock").open("a+b") as stream:
        acquire_exclusive_file_lock(stream)
        try:
            intent = _read_intent(path, session_id, request_id)
            without_marker, occurrences = _replace_marker(body, intent["marker"], "")
            if intent["native_prompt_id"] is not None:
                if not isinstance(intent["decision"], dict) or not isinstance(
                    intent["instructions"], str
                ):
                    raise ownership.OwnershipProtocolError(
                        "ownership capture did not finish; refusing a duplicate charge"
                    )
                if intent["native_prompt_id"] != prompt_id:
                    _bind_continuation(session_id, request_id, prompt_id, env)
                _install_decision(session_id, intent["request"], intent["decision"], env)
                return _inject_instructions(body, intent["marker"], intent["instructions"])
            if current["dispatch_status"] != "undecided" or not occurrences:
                raise ownership.OwnershipStateError(
                    "main request is missing its fresh ownership marker"
                )
            request = copy.deepcopy(intent["request"])
            request.pop("input_sha256", None)
            projected = project_request(without_marker)
            projected["attachments"] = request["task"]["attachments"] + projected["attachments"]
            projected["context_complete"] &= all(
                item["available"] for item in projected["attachments"]
            )
            request["task"].update(projected)
            request["existing_owners"] = [
                owner for owner in state["owners"] if owner["state"] != "failed"
            ]
            request = ownership.validate_request(request)
            # Persist admission before networking. A crashed/ambiguous call is never retried silently.
            intent.update(
                request=request,
                native_prompt_id=prompt_id,
                captured_input_sha256=request["input_sha256"],
            )
            atomic_write_json(path, intent)
            decision = ownership.decide_ownership(workspace, request, token=token, state=state)
            instructions = ownership.instructions_for_decision(
                decision, answer_delivery=request["constraints"]["answer_delivery"]
            )
            intent.update(decision=decision, instructions=instructions)
            atomic_write_json(path, intent)
            _install_decision(session_id, request, decision, env)
            return _replace_marker(body, intent["marker"], instructions)[0]
        finally:
            release_file_lock(stream)
