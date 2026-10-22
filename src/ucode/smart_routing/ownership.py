"""Authenticated Python bridge for ownership decisions and session state."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ucode.claude_answer_delivery import AVAILABLE_CONTEXT, UNAVAILABLE_CONTEXT, parse_selection
from ucode.config_io import atomic_write_json
from ucode.databricks import get_databricks_token
from ucode.os_compatibility.file_lock_cross_os import (
    acquire_exclusive_file_lock,
    release_file_lock,
)

POLICY_VERSION = "orchestration_v1"
POLICY_PATH = Path(__file__).with_name("policies") / f"{POLICY_VERSION}.json"
POLICY_SHA256 = "adca6db0480b54494fa76014d72076f020cc4a7a2f66527b54e3671a9980b1fa"
DECISION_PATH = "/ai-gateway/routing/v1/orchestration:decide"
REQUEST_TIMEOUT_S = 20.0
MAX_BODY_BYTES = 256 * 1024
MAX_BRIDGE_BODY_BYTES = 2 * MAX_BODY_BYTES
MAX_ERROR_BODY_BYTES = MAX_BODY_BYTES
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DISPATCH_VALUES = {"work_directly", "spawn_subagent", "reuse_subagent"}
_DISPATCH_STATUSES = {
    "undecided",
    "awaiting_dispatch",
    "started",
    "completed",
    "failed",
}
_STATE_TOP_LEVEL = {"schema_version", "session_id", "generation", "current", "owners"}
_CURRENT_REQUIRED = {
    "request_id",
    "input_sha256",
    "policy_version",
    "policy_sha256",
    "decision_status",
    "ownership",
    "dispatch",
    "owner_agent_id",
    "dispatch_status",
}
_CURRENT_OPTIONAL = {
    "turn_id",
    "agent_tool_use_id",
    "continuation_turn_id",
    "continuation_prompt_id",
}
_OWNER_KEYS = {"agent_id", "scope", "state"}
_REQUEST_REQUIRED = {
    "schema_version",
    "policy_version",
    "request_id",
    "task",
    "constraints",
    "existing_owners",
}
_REQUEST_OPTIONAL = {"input_sha256"}
_RESPONSE_REQUIRED = {
    "schema_version",
    "policy_version",
    "request_id",
    "input_sha256",
    "decision_status",
    "ownership",
    "dispatch",
    "owner_agent_id",
    "reason_code",
    "policy_sha256",
}
_RESPONSE_OPTIONAL = {"usage_receipt"}
_RECEIPT_KEYS = {
    "operation",
    "request_id",
    "classifier_id",
    "provider_request_id",
    "usage",
    "finish_reason",
    "status",
}
_RECEIPT_STATUSES = {"ok", "unavailable", "truncated", "cancelled", "error"}


class OwnershipError(RuntimeError):
    """Base error for ownership setup, wire, and state failures."""


class OwnershipSetupError(OwnershipError):
    """The local policy or launch configuration is not compatible."""


class OwnershipProtocolError(OwnershipError):
    """The service or native bridge violated the ownership protocol."""

    def __init__(self, message: str, *, usage_receipt: dict[str, Any] | None = None):
        super().__init__(message)
        self.usage_receipt = usage_receipt


class OwnershipServiceError(OwnershipError):
    """The ownership service could not classify the request."""


class OwnershipStateError(OwnershipError):
    """Session ownership state is invalid or stale."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise OwnershipProtocolError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _json_loads(data: str | bytes, *, max_bytes: int = MAX_BODY_BYTES) -> Any:
    def reject_constant(value: str) -> None:
        raise OwnershipProtocolError(f"non-finite JSON number is not allowed: {value}")

    try:
        if len(data) > max_bytes:
            limit_kib = max_bytes // 1024
            raise OwnershipProtocolError(
                f"ownership protocol JSON exceeds the {limit_kib} KiB limit"
            )
        return json.loads(data, object_pairs_hook=_strict_object, parse_constant=reject_constant)
    except OwnershipProtocolError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise OwnershipProtocolError("invalid JSON in ownership protocol") from exc


def _object(
    value: object,
    label: str,
    required: set[str],
    optional: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OwnershipProtocolError(f"{label} must be an object")
    keys = set(value)
    missing = required - keys
    unknown = keys - required - optional
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing {sorted(missing)}")
        if unknown:
            details.append(f"unknown {sorted(unknown)}")
        raise OwnershipProtocolError(f"invalid {label}: {'; '.join(details)}")
    return value


def _nonempty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OwnershipProtocolError(f"{label} must be a non-empty string")
    return value


def _sha256_string(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise OwnershipProtocolError(f"{label} must be a lowercase SHA-256")
    return value


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise OwnershipProtocolError("ownership request is not canonical JSON") from exc


def request_sha256(request: Mapping[str, Any]) -> str:
    """Hash the canonical request fields, excluding the supplied digest."""
    unhashed = {key: value for key, value in request.items() if key != "input_sha256"}
    return hashlib.sha256(_canonical_json(unhashed)).hexdigest()


def load_policy() -> dict[str, Any]:
    """Load only the packaged policy whose bytes match the source pin."""
    try:
        raw = POLICY_PATH.read_bytes()
    except OSError as exc:
        raise OwnershipSetupError(f"ownership policy bundle is missing: {POLICY_PATH}") from exc
    digest = hashlib.sha256(raw).hexdigest()
    if digest != POLICY_SHA256:
        raise OwnershipSetupError(
            f"ownership policy SHA-256 mismatch: expected {POLICY_SHA256}, got {digest}"
        )
    policy = _json_loads(raw)
    if not isinstance(policy, dict) or policy.get("policy_version") != POLICY_VERSION:
        raise OwnershipSetupError("ownership policy version is missing or unsupported")
    for key in ("activation", "parent_instructions"):
        if not isinstance(policy.get(key), str) or not policy[key]:
            raise OwnershipSetupError(f"ownership policy {key!r} is missing")
    if not isinstance(policy.get("roles"), dict) or not policy["roles"]:
        raise OwnershipSetupError("ownership policy roles are missing")
    if not isinstance(policy.get("classifier_id"), str) or not policy["classifier_id"].strip():
        raise OwnershipSetupError("ownership policy classifier_id binding is missing")
    if not isinstance(policy.get("reason_codes"), list) or not all(
        isinstance(code, str) and code for code in policy["reason_codes"]
    ):
        raise OwnershipSetupError("ownership policy reason codes are invalid")
    return policy


def policy_sha256() -> str:
    """Return the verified byte digest of the packaged ownership policy."""
    load_policy()
    return POLICY_SHA256


def validate_request(request: object) -> dict[str, Any]:
    """Validate and complete a bridge request before sending it to the service."""
    value = _object(request, "request", _REQUEST_REQUIRED, _REQUEST_OPTIONAL)
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise OwnershipProtocolError("unsupported ownership request schema_version")
    if value["policy_version"] != POLICY_VERSION:
        raise OwnershipProtocolError("ownership request policy_version does not match the bundle")
    _nonempty_string(value["request_id"], "request_id")

    task = _object(
        value["task"],
        "task",
        {"prompt", "context", "context_complete", "attachments"},
    )
    if not isinstance(task["prompt"], str) or not task["prompt"].strip():
        raise OwnershipProtocolError("task.prompt must be a non-empty string")
    if type(task["context_complete"]) is not bool:
        raise OwnershipProtocolError("task.context_complete must be a boolean")
    if not isinstance(task["context"], list):
        raise OwnershipProtocolError("task.context must be a list")
    for index, item in enumerate(task["context"]):
        block = _object(item, f"task.context[{index}]", {"role", "content"})
        if block["role"] not in {"user", "assistant", "tool", "system"}:
            raise OwnershipProtocolError(f"task.context[{index}].role is unsupported")
        if not isinstance(block["content"], str):
            raise OwnershipProtocolError(f"task.context[{index}].content must be a string")
    if not isinstance(task["attachments"], list):
        raise OwnershipProtocolError("task.attachments must be a list")
    for index, attachment in enumerate(task["attachments"]):
        item = _object(attachment, f"task.attachments[{index}]", {"name", "available"})
        _nonempty_string(item["name"], f"task.attachments[{index}].name")
        if type(item["available"]) is not bool:
            raise OwnershipProtocolError(f"task.attachments[{index}].available must be a boolean")

    constraints = _object(
        value["constraints"],
        "constraints",
        {
            "root_session",
            "subagents_allowed",
            "externally_assigned",
            "routing_enabled",
            "answer_delivery",
            "available_tools",
        },
    )
    for key in (
        "root_session",
        "subagents_allowed",
        "externally_assigned",
        "routing_enabled",
        "answer_delivery",
    ):
        if type(constraints[key]) is not bool:
            raise OwnershipProtocolError(f"constraints.{key} must be a boolean")
    tools = constraints["available_tools"]
    if not isinstance(tools, list) or not all(isinstance(tool, str) and tool for tool in tools):
        raise OwnershipProtocolError(
            "constraints.available_tools must be a list of non-empty strings"
        )

    if not isinstance(value["existing_owners"], list):
        raise OwnershipProtocolError("existing_owners must be a list")
    seen_owners: set[str] = set()
    for index, owner in enumerate(value["existing_owners"]):
        item = _object(owner, f"existing_owners[{index}]", {"agent_id", "scope", "state"})
        agent_id = _nonempty_string(item["agent_id"], f"existing_owners[{index}].agent_id")
        _nonempty_string(item["scope"], f"existing_owners[{index}].scope")
        if item["state"] not in {"running", "completed", "failed"}:
            raise OwnershipProtocolError(f"existing_owners[{index}].state is unsupported")
        if agent_id in seen_owners:
            raise OwnershipProtocolError("existing_owners contains duplicate agent IDs")
        seen_owners.add(agent_id)

    digest = request_sha256(value)
    supplied_digest = value.get("input_sha256")
    if supplied_digest is not None and _sha256_string(supplied_digest, "input_sha256") != digest:
        raise OwnershipProtocolError("ownership request input_sha256 does not match its content")
    value["input_sha256"] = digest
    return value


def _validate_receipt(
    receipt: object, request: Mapping[str, Any], policy: Mapping[str, Any]
) -> dict[str, Any]:
    value = _object(
        receipt,
        "usage_receipt",
        _RECEIPT_KEYS,
        {"failure_reason", "provider_model", "response_sha256"},
    )
    if value["operation"] != "orchestration_decision":
        raise OwnershipProtocolError("usage_receipt operation does not match")
    if value["request_id"] != request["request_id"]:
        raise OwnershipProtocolError("usage_receipt request_id does not match")
    classifier_id = _nonempty_string(value["classifier_id"], "usage_receipt.classifier_id")
    trusted_classifier_id = policy["classifier_id"]
    if classifier_id != trusted_classifier_id:
        raise OwnershipProtocolError(
            "usage_receipt classifier_id does not match the local policy pin"
        )
    provider_request_id = value["provider_request_id"]
    if provider_request_id is not None:
        _nonempty_string(provider_request_id, "usage_receipt.provider_request_id")
    if value["usage"] is not None and not isinstance(value["usage"], dict):
        raise OwnershipProtocolError("usage_receipt.usage must be an object or null")
    if value["finish_reason"] is not None and not isinstance(value["finish_reason"], str):
        raise OwnershipProtocolError("usage_receipt.finish_reason must be a string or null")
    status = value["status"]
    if not isinstance(status, str) or status not in _RECEIPT_STATUSES:
        raise OwnershipProtocolError("usage_receipt.status is unsupported")
    if "failure_reason" in value:
        reason = _nonempty_string(value["failure_reason"], "usage_receipt.failure_reason")
        if len(reason) > 256:
            raise OwnershipProtocolError("usage_receipt.failure_reason exceeds limit")
    if value.get("provider_model") is not None:
        _nonempty_string(value["provider_model"], "usage_receipt.provider_model")
    if value.get("response_sha256") is not None:
        _sha256_string(value["response_sha256"], "usage_receipt.response_sha256")
    return value


def validate_decision(
    response: object,
    request: Mapping[str, Any],
    *,
    policy: Mapping[str, Any] | None = None,
    state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate service identity, policy, action, owner reuse, and accounting."""
    bundle = load_policy() if policy is None else policy
    value = _object(response, "decision", _RESPONSE_REQUIRED, _RESPONSE_OPTIONAL)
    expected = {
        "schema_version": 1,
        "policy_version": POLICY_VERSION,
        "request_id": request["request_id"],
        "input_sha256": request["input_sha256"],
        "policy_sha256": policy_sha256(),
    }
    for key, expected_value in expected.items():
        if value[key] != expected_value:
            raise OwnershipProtocolError(f"ownership decision {key} does not match the request")
    if type(value["schema_version"]) is not int:
        raise OwnershipProtocolError("ownership decision schema_version must be an integer")
    if value["decision_status"] == "ok" and "usage_receipt" not in value:
        raise OwnershipProtocolError("successful ownership decisions must include usage_receipt")
    receipt = value.get("usage_receipt")
    if receipt is not None:
        value["usage_receipt"] = _validate_receipt(receipt, request, bundle)

    if value["decision_status"] != "ok":
        raise OwnershipProtocolError("ownership decision_status is unsupported")
    ownership = value["ownership"]
    dispatch = value["dispatch"]
    owner_agent_id = value["owner_agent_id"]
    pairs = {
        ("main", "work_directly"),
        ("subagent", "spawn_subagent"),
        ("subagent", "reuse_subagent"),
    }
    if (ownership, dispatch) not in pairs:
        raise OwnershipProtocolError("ownership and dispatch combination is unsupported")
    if value["reason_code"] not in bundle["reason_codes"]:
        raise OwnershipProtocolError("ownership decision reason_code is not in the local policy")
    if ownership == "subagent":
        constraints = request["constraints"]
        has_native_agent = bool({"Agent", "Task"}.intersection(constraints["available_tools"]))
        if (
            not constraints["root_session"]
            or not constraints["subagents_allowed"]
            or constraints["externally_assigned"]
            or not constraints["routing_enabled"]
            or not has_native_agent
        ):
            raise OwnershipProtocolError("subagent decision violates structured launch constraints")
    if dispatch in {"work_directly", "spawn_subagent"}:
        if owner_agent_id is not None:
            raise OwnershipProtocolError("new decisions cannot invent an owner_agent_id")
    else:
        owner_id = _nonempty_string(owner_agent_id, "owner_agent_id")
        request_owners = {
            owner["agent_id"]: owner
            for owner in request["existing_owners"]
            if owner["state"] in {"running", "completed"}
        }
        local_owners = {
            owner["agent_id"]: owner
            for owner in (state or {}).get("owners", [])
            if owner.get("state") in {"running", "completed"}
        }
        request_owner = request_owners.get(owner_id)
        local_owner = local_owners.get(owner_id)
        if (
            request_owner is None
            or local_owner is None
            or request_owner["scope"] != local_owner["scope"]
        ):
            raise OwnershipProtocolError(
                "reuse owner is not a fresh same-session running or completed owner"
            )
    return value


def instructions_for_decision(
    decision: Mapping[str, Any],
    policy: Mapping[str, Any] | None = None,
    *,
    answer_delivery: bool,
) -> str:
    """Return workflow, the validated action, and launch-authorized answer delivery."""
    if type(answer_delivery) is not bool:
        raise OwnershipProtocolError("answer_delivery must be a boolean")
    if not {"request_id", "decision_status", "ownership", "dispatch", "owner_agent_id"} <= set(
        decision
    ):
        raise OwnershipProtocolError("ownership decision is incomplete for runtime instructions")

    decision_status = decision["decision_status"]
    ownership = decision["ownership"]
    dispatch = decision["dispatch"]
    owner_agent_id = decision["owner_agent_id"]
    has_dispatch_status = "dispatch_status" in decision
    dispatch_status = decision.get("dispatch_status")
    if has_dispatch_status and (
        not isinstance(dispatch_status, str) or dispatch_status not in _DISPATCH_STATUSES
    ):
        raise OwnershipProtocolError("ownership dispatch_status is unsupported")
    if decision_status == "ok":
        if not isinstance(ownership, str) or not isinstance(dispatch, str):
            raise OwnershipProtocolError("ownership decision action is unsupported")
        if (ownership, dispatch) not in {
            ("main", "work_directly"),
            ("subagent", "spawn_subagent"),
            ("subagent", "reuse_subagent"),
        }:
            raise OwnershipProtocolError("ownership decision action is unsupported")
        if dispatch == "reuse_subagent":
            _nonempty_string(owner_agent_id, "owner_agent_id")
        elif owner_agent_id is not None:
            _nonempty_string(owner_agent_id, "owner_agent_id")
            if not (
                has_dispatch_status
                and dispatch == "spawn_subagent"
                and dispatch_status in {"started", "completed", "failed"}
            ):
                raise OwnershipProtocolError(
                    "ownership decision owner_agent_id is not a native owner"
                )
    else:
        raise OwnershipProtocolError("ownership decision_status is unsupported")

    request_id = _nonempty_string(decision["request_id"], "request_id")
    action = {
        "request_id": request_id,
        "decision_status": decision_status,
        "ownership": ownership,
        "dispatch": dispatch,
        "owner_agent_id": owner_agent_id,
    }
    if has_dispatch_status:
        action["dispatch_status"] = dispatch_status

    bundle = load_policy() if policy is None else policy
    delivery_marker = "AVAILABLE" if answer_delivery else "UNAVAILABLE"
    delivery_context = AVAILABLE_CONTEXT if answer_delivery else UNAVAILABLE_CONTEXT
    sections = [
        f"claude_answer_delivery: {delivery_marker}\n{delivery_context}",
        bundle["activation"],
    ]
    sections.append(bundle["parent_instructions"])
    summary = json.dumps(
        action, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )
    sections.append(f"Validated ownership decision (runtime JSON):\n{summary}")
    return "\n\n".join(sections)


def _gateway_protocol_error(status: int, body: bytes) -> dict[str, Any] | None:
    if len(body) > MAX_ERROR_BODY_BYTES:
        return None
    try:
        error = _json_loads(body)
    except OwnershipProtocolError:
        return None
    if not isinstance(error, dict):
        return None
    if error.get("error_code") == "INVALID_ORCHESTRATION_DECISION_RESPONSE":
        return error
    if (
        status == 500
        and error.get("error_code") == "INTERNAL_ERROR"
        and error.get("message") == "Invalid orchestration decision response"
    ):
        return error
    return None


def _read_state_unlocked(path: Path, session_id: str) -> dict[str, Any] | None:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise OwnershipStateError(f"could not read ownership state: {exc}") from exc
    try:
        state = _json_loads(raw)
        return _validate_state(state, session_id)
    except OwnershipProtocolError as exc:
        raise OwnershipStateError(f"ownership state is invalid: {exc}") from exc


def _state_paths(session_id: str, env: Mapping[str, str] | None = None) -> tuple[Path, Path]:
    _nonempty_string(session_id, "session_id")
    from ucode.agents.claude import claude_config_dir

    session_key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    state_dir = claude_config_dir(env) / "ucode" / "orchestration-state"
    return state_dir / f"{session_key}.json", state_dir / f"{session_key}.lock"


def _validate_state(state: object, session_id: str) -> dict[str, Any]:
    value = _object(state, "state", _STATE_TOP_LEVEL)
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise OwnershipProtocolError("unsupported ownership state schema_version")
    if value["session_id"] != session_id:
        raise OwnershipProtocolError("ownership state session_id does not match")
    if type(value["generation"]) is not int or value["generation"] < 0:
        raise OwnershipProtocolError("ownership state generation must be a non-negative integer")
    current = value["current"]
    if current is not None:
        record = _object(current, "state.current", _CURRENT_REQUIRED, _CURRENT_OPTIONAL)
        _nonempty_string(record["request_id"], "state.current.request_id")
        _nonempty_string(record["policy_version"], "state.current.policy_version")
        _sha256_string(record["policy_sha256"], "state.current.policy_sha256")
        decision_status = record["decision_status"]
        if decision_status not in {"undecided", "ok"}:
            raise OwnershipProtocolError("state.current.decision_status is unsupported")
        if decision_status == "undecided":
            if record["input_sha256"] is not None:
                raise OwnershipProtocolError("undecided state.current.input_sha256 must be null")
            if (
                any(record[key] is not None for key in ("ownership", "dispatch", "owner_agent_id"))
                or record["dispatch_status"] != "undecided"
            ):
                raise OwnershipProtocolError("undecided ownership state must not select an action")
        else:
            _sha256_string(record["input_sha256"], "state.current.input_sha256")
        if record["policy_version"] != POLICY_VERSION or record["policy_sha256"] != policy_sha256():
            raise OwnershipProtocolError(
                "state.current policy binding does not match the local bundle"
            )
        if record["ownership"] not in {None, "main", "subagent"}:
            raise OwnershipProtocolError("state.current.ownership is unsupported")
        if record["dispatch"] not in {None, *_DISPATCH_VALUES}:
            raise OwnershipProtocolError("state.current.dispatch is unsupported")
        if record["owner_agent_id"] is not None:
            _nonempty_string(record["owner_agent_id"], "state.current.owner_agent_id")
        if record["dispatch_status"] not in _DISPATCH_STATUSES:
            raise OwnershipProtocolError("state.current.dispatch_status is unsupported")
        for key in _CURRENT_OPTIONAL:
            if key in record and record[key] is not None:
                _nonempty_string(record[key], f"state.current.{key}")
        if (
            record.get("continuation_prompt_id") is not None
            and record.get("continuation_turn_id") is None
        ):
            raise OwnershipProtocolError(
                "continuation_prompt_id requires a continuation_turn_id permit"
            )
    owners = value["owners"]
    if not isinstance(owners, list):
        raise OwnershipProtocolError("state.owners must be a list")
    seen: set[str] = set()
    for index, owner in enumerate(owners):
        item = _object(owner, f"state.owners[{index}]", _OWNER_KEYS)
        agent_id = _nonempty_string(item["agent_id"], f"state.owners[{index}].agent_id")
        _nonempty_string(item["scope"], f"state.owners[{index}].scope")
        if item["state"] not in {"running", "completed", "failed"}:
            raise OwnershipProtocolError(f"state.owners[{index}].state is unsupported")
        if agent_id in seen:
            raise OwnershipProtocolError("state.owners contains duplicate agent IDs")
        seen.add(agent_id)
    return value


def read_state(session_id: str, *, env: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    """Read one session's state under the existing cross-platform lock."""
    path, lock_path = _state_paths(session_id, env)
    path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        acquire_exclusive_file_lock(lock_file)
        try:
            return _read_state_unlocked(path, session_id)
        finally:
            release_file_lock(lock_file)


def write_state(
    session_id: str,
    state: object,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Atomically persist monotonic state; equal-generation identical retries are idempotent."""
    try:
        value = _validate_state(state, session_id)
    except OwnershipProtocolError as exc:
        raise OwnershipStateError(str(exc)) from exc
    path, lock_path = _state_paths(session_id, env)
    path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        acquire_exclusive_file_lock(lock_file)
        try:
            prior = _read_state_unlocked(path, session_id)
            if prior is not None:
                if value["generation"] < prior["generation"]:
                    raise OwnershipStateError("ownership state generation is stale")
                if value["generation"] == prior["generation"] and value != prior:
                    raise OwnershipStateError(
                        "ownership state generation conflicts with stored state"
                    )
            atomic_write_json(path, value)
        finally:
            release_file_lock(lock_file)
    return value


def decide_ownership(
    workspace: str,
    request: object,
    *,
    token: str | None = None,
    timeout: float = REQUEST_TIMEOUT_S,
    state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Call the public ownership endpoint and return a validated decision."""
    bundle = load_policy()
    value = validate_request(request)
    digest = policy_sha256()
    if not workspace.startswith(("https://", "http://")):
        raise OwnershipSetupError("workspace must be an absolute HTTP(S) URL")
    attachments = [item["name"] for item in value["task"]["attachments"] if not item["available"]]
    if attachments:
        raise OwnershipServiceError(
            "Ownership decisions cannot classify attachments: " + ", ".join(attachments)
        )
    if not value["task"]["context_complete"]:
        raise OwnershipServiceError("Ownership decisions cannot classify this request's content.")
    body = _canonical_json(value)
    if len(body) > MAX_BODY_BYTES:
        raise OwnershipServiceError(
            f"Ownership request is {len(body)} bytes; the service limit is {MAX_BODY_BYTES}."
        )
    bearer = token or get_databricks_token(workspace)
    request_obj = urllib.request.Request(
        workspace.rstrip("/") + DECISION_PATH,
        data=body,
        headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirectHandler())
    try:
        with opener.open(request_obj, timeout=timeout) as response:
            response_body = response.read(MAX_BODY_BYTES + 1)
            payload = _json_loads(response_body)
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise OwnershipProtocolError(
                "ownership decision endpoint redirects are not allowed"
            ) from exc
        try:
            error_body = exc.read(MAX_ERROR_BODY_BYTES + 1)
        except OSError:
            error_body = b""
        gateway_error = _gateway_protocol_error(exc.code, error_body)
        if gateway_error is not None:
            receipt = None
            if gateway_error.get("usage_receipt") is not None:
                try:
                    receipt = _validate_receipt(gateway_error["usage_receipt"], value, bundle)
                except OwnershipProtocolError as receipt_error:
                    raise OwnershipProtocolError(
                        "Gateway rejected the classifier response as a protocol error; "
                        f"its usage_receipt failed validation: {receipt_error}"
                    ) from exc
            raise OwnershipProtocolError(
                "Gateway rejected the classifier response as a protocol error",
                usage_receipt=receipt,
            ) from exc
        raise OwnershipServiceError(f"Ownership service returned HTTP {exc.code}.") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise OwnershipServiceError(f"Ownership service is unreachable: {exc}") from exc
    decision = validate_decision(payload, value, policy=bundle, state=state)
    if decision["policy_sha256"] != digest:
        raise OwnershipProtocolError("ownership decision policy hash changed during validation")
    return decision


def _bridge(
    envelope: object, *, workspace: str | None, env: Mapping[str, str] | None
) -> dict[str, Any]:
    value = _object(envelope, "bridge envelope", {"operation", "session_id"}, {"request", "state"})
    operation = value["operation"]
    session_id = _nonempty_string(value["session_id"], "session_id")
    if operation == "validate_answer":
        if set(value) != {"operation", "session_id", "request"}:
            raise OwnershipProtocolError("validate_answer requires only a request payload")
        request = _object(value["request"], "answer validation request", {"final_text"})
        if not isinstance(request["final_text"], str):
            raise OwnershipProtocolError("final_text must be a string")
        _, error = parse_selection(request["final_text"])
        return {"error": error}
    if operation == "state_read":
        if set(value) != {"operation", "session_id"}:
            raise OwnershipProtocolError("state_read does not accept request or state")
        return {"state": read_state(session_id, env=env)}
    if operation == "state_write":
        if set(value) != {"operation", "session_id", "state"}:
            raise OwnershipProtocolError("state_write requires only a state payload")
        return {"state": write_state(session_id, value["state"], env=env)}
    if operation == "prepare":
        if set(value) != {"operation", "session_id", "request"}:
            raise OwnershipProtocolError("prepare requires only a request payload")
        from ucode.smart_routing.ownership_transport import prepare_request

        return prepare_request(value["request"], session_id, env=env)
    if operation != "decide" or set(value) != {"operation", "session_id", "request"}:
        raise OwnershipProtocolError("unsupported ownership bridge operation or fields")
    raise OwnershipProtocolError(
        "decide is not a native bridge operation; decisions bind to the captured gateway request"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace")
    args = parser.parse_args()
    try:
        envelope = _json_loads(
            sys.stdin.buffer.read(MAX_BRIDGE_BODY_BYTES + 1),
            max_bytes=MAX_BRIDGE_BODY_BYTES,
        )
        result = _bridge(envelope, workspace=args.workspace, env=os.environ)
    except (OwnershipError, OSError, ValueError) as exc:
        error = {"error": str(exc)}
        if isinstance(exc, OwnershipProtocolError) and exc.usage_receipt is not None:
            error["usage_receipt"] = exc.usage_receipt
        print(json.dumps(error, ensure_ascii=False, allow_nan=False), file=sys.stdout)
        raise SystemExit(2) from exc
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
