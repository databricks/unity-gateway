"""Shared Unity Gateway routing helpers for coding-agent sessions and subagents.

Both the Codex and Claude Code integrations route through the workspace's
configured router at ``/ai-gateway/routing/v1/routes:select``. The
harness-agnostic mechanics live here — the gateway call, the decision shape,
and the canary/audit/decision bookkeeping. Each harness supplies its own route
options, spawn-tool detector, model-id translation, and artifact paths.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import textwrap
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ucode.smart_routing.rationale import format_rationale
from ucode.ug_headers import ug_headers
from ucode.workspace import workspace_headers

ROUTER_NAME = "stable"
ROUTER_NAME_ENV_VAR = "SMART_ROUTER_NAME"
ROUTING_PATH = "/ai-gateway/routing/v1/routes:select"
REQUEST_TIMEOUT_S = 30.0
SUBAGENT_ROUTING_DISCLAIMER = (
    "Spawned subagents are routed independently based on their own complexity."
)
ROUTING_BOX_WIDTH = 73
_ANTHROPIC_AIGW_MODEL_RE = re.compile(r"^anthropic-aigw-[0-9a-fA-F]{7,8}-(.+)$")
_FERNET_TOKEN_RE = re.compile(r"^gAAAAA[A-Za-z0-9_-]+={0,2}$")
_EXPLANATION_MODEL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,159}")


def format_switch_message(model: str, reason: str | None) -> str:
    """Format the first-prompt routed-model notice."""
    lines = [
        "Using Unity Gateway Smart Router.",
        f"Selected Model : {model}",
        *([f"Reason : {reason}"] if reason else []),
        SUBAGENT_ROUTING_DISCLAIMER,
    ]
    return _format_box(lines)


def format_subagent_message(
    model: str,
    reason: str | None,
    *,
    subagent_name: str | None = None,
    prompt: str | None = None,
) -> str:
    """Format a routed-subagent notice without the first-prompt disclaimer."""
    lines = [
        "Using Unity Gateway Smart Router - Subagent",
        *(
            [f"Subagent : {subagent_name}"]
            if subagent_name
            else [f"Prompt : {prompt}"]
            if prompt
            else []
        ),
        f"Selected Model : {model}",
        *([f"Reason : {reason}"] if reason else []),
    ]
    return _format_box(lines)


def _format_box(lines: list[str]) -> str:
    wrapped_lines = [
        wrapped
        for line in lines
        for wrapped in (textwrap.wrap(line, width=ROUTING_BOX_WIDTH) or [""])
    ]
    border = "─" * (ROUTING_BOX_WIDTH + 2)
    return "\n".join(
        [
            f"┌{border}┐",
            *(f"│ {line:<{ROUTING_BOX_WIDTH}} │" for line in wrapped_lines),
            f"└{border}┘",
        ]
    )


@dataclass(frozen=True)
class RoutingDecision:
    """One model selection returned by the Unity Gateway router."""

    model: str
    rationale: str = ""
    route_options: tuple[tuple[str, str | None], ...] = ()
    router_name: str = ""
    raw_rationale: str | None = None
    explanation: dict[str, Any] | None = None

    def display_message(
        self,
        model_label: str | None = None,
        *,
        is_subagent: bool = False,
        subagent_name: str | None = None,
        prompt: str | None = None,
    ) -> str:
        """Return the boxed smart-routing notice with the router's rationale.

        Used by both the launch-time notice and the subagent-routing hook so the
        "what" (model) and the "why" (rationale) are surfaced consistently.
        ``model_label`` overrides the shown model id (e.g. a harness-translated
        id); defaults to ``model``.
        """
        if is_subagent:
            return format_subagent_message(
                model_label or self.model,
                self.rationale,
                subagent_name=subagent_name,
                prompt=prompt,
            )
        return format_switch_message(model_label or self.model, self.rationale)


@dataclass(frozen=True)
class SpawnRoute:
    tool_input: dict[str, Any]
    task: str
    decision: RoutingDecision
    routed_model: str


@dataclass(frozen=True)
class SubagentNoticeConfig:
    """Harness-specific fields and model formatting for subagent notices."""

    name_field: str
    prompt_field: str
    display_model_mapper: Callable[[str], str] | None = None
    leading_newline: bool = False
    name_suffix: str = ""

    def name(self, tool_input: dict[str, Any]) -> str | None:
        return _nonempty_string(tool_input.get(self.name_field))

    def prompt(self, tool_input: dict[str, Any]) -> str | None:
        return _nonempty_string(tool_input.get(self.prompt_field))

    def display_model(self, decision_model: str, routed_model: str) -> str:
        if self.display_model_mapper is None:
            return routed_model
        display_model = self.display_model_mapper(decision_model)
        return display_model if display_model != decision_model else routed_model

    def message(
        self,
        decision: RoutingDecision,
        routed_model: str,
        tool_input: dict[str, Any],
    ) -> str:
        subagent_name = self.name(tool_input)
        if self.name_suffix:
            subagent_name = f"{subagent_name or 'subagent'}{self.name_suffix}"
        message = decision.display_message(
            model_label=self.display_model(decision.model, routed_model),
            is_subagent=True,
            subagent_name=subagent_name,
            prompt=self.prompt(tool_input),
        )
        return f"\n{message}" if self.leading_newline else message


def _nonempty_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _plaintext_string(value: Any) -> str | None:
    text = _nonempty_string(value)
    if text is None or _FERNET_TOKEN_RE.fullmatch(text):
        return None
    return text


def unwrap_anthropic_gateway_model(model: str) -> str:
    """Strip Claude's synthetic Anthropic gateway prefix from a model id."""
    if match := _ANTHROPIC_AIGW_MODEL_RE.fullmatch(model):
        return match.group(1)
    return model


def _explanation_model_id(model: str) -> str:
    model = unwrap_anthropic_gateway_model(model)
    if model.startswith("system.ai."):
        return model.removeprefix("system.ai.").removesuffix("[1m]")
    # Brackets can be part of a catalog-qualified custom service name.
    return model if len(model.split(".")) >= 3 else model.removesuffix("[1m]")


def _safe_explanation_text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
    clean = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()[:limit]
    return clean or None


def _valid_explanation_number(value: Any, *, non_negative: bool = False) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and abs(value) <= 1e12
        and math.isfinite(value)
        and (not non_negative or value >= 0)
    )


def _valid_explanation_details(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and len(value) <= 8
        and all(
            isinstance(key, str)
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9_. -]{0,63}", key)
            and _valid_explanation_number(number)
            for key, number in value.items()
        )
    )


def _parse_routing_explanation(
    value: Any,
    routed_model: str,
    offered_routes: tuple[tuple[str, str | None], ...],
) -> dict[str, Any] | None:
    if isinstance(value, str):
        try:
            if len(value.encode("utf-8")) > 65536:
                return None
            value = json.loads(value)
        except (UnicodeEncodeError, ValueError, RecursionError):
            return None
    if not isinstance(value, dict) or len(value) > 32:
        return None
    try:
        if len(json.dumps(value, allow_nan=False).encode("utf-8")) > 65536:
            return None
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        return None

    selected_model = value.get("selected_model")
    summary = _safe_explanation_text(value.get("summary"), 2000)
    task_understanding = value.get("task_understanding")
    models = value.get("models")
    if not isinstance(task_understanding, list) or len(task_understanding) > 8:
        return None
    normalized_task_understanding = []
    for item in task_understanding:
        text = _safe_explanation_text(item, 240)
        if not text:
            return None
        normalized_task_understanding.append(text)
    if (
        not isinstance(selected_model, str)
        or not _EXPLANATION_MODEL_ID_RE.fullmatch(selected_model)
        or selected_model != _explanation_model_id(routed_model)
        or not summary
        or not isinstance(models, list)
        or len(models) < 1
    ):
        return None

    offered_models = {_explanation_model_id(model) for model, _ in offered_routes}
    model_ids: list[str] = []
    selected_ids: list[str] = []
    numeric_fields = {
        "quality_estimate": False,
        "estimated_cost_usd": True,
        "routing_score": False,
    }
    detail_fields = ("components", "difference_from_winner")
    normalized_models = []
    for candidate in models:
        if not isinstance(candidate, dict):
            return None
        model_id = candidate.get("id")
        name = _safe_explanation_text(candidate.get("name"), 128)
        reason = _safe_explanation_text(candidate.get("reason"), 1200)
        selected = candidate.get("selected")
        considered = candidate.get("considered")
        if (
            not isinstance(model_id, str)
            or not _EXPLANATION_MODEL_ID_RE.fullmatch(model_id)
            or not name
            or not reason
            or not isinstance(selected, bool)
            or not isinstance(considered, bool)
            or (selected and not considered)
        ):
            return None
        model_ids.append(model_id)
        if selected:
            selected_ids.append(model_id)
        if any(
            field in candidate
            and not _valid_explanation_number(candidate[field], non_negative=non_negative)
            for field, non_negative in numeric_fields.items()
        ):
            return None
        if any(
            field in candidate and not _valid_explanation_details(candidate[field])
            for field in detail_fields
        ):
            return None
        normalized_candidate = {
            "id": model_id,
            "name": name,
            "selected": selected,
            "considered": considered,
            "reason": reason,
        }
        normalized_candidate.update(
            {field: candidate[field] for field in numeric_fields if field in candidate}
        )
        normalized_candidate.update(
            {field: dict(candidate[field]) for field in detail_fields if field in candidate}
        )
        normalized_models.append(normalized_candidate)

    if (
        len(model_ids) != len(offered_models)
        or set(model_ids) != offered_models
        or len(model_ids) != len(set(model_ids))
        or selected_ids != [selected_model]
    ):
        return None
    normalized = {
        "selected_model": selected_model,
        "summary": summary,
        "task_understanding": normalized_task_understanding,
        "models": normalized_models,
    }
    for field, limit in (("recipe", 240), ("formula", 500)):
        if field in value:
            text = _safe_explanation_text(value[field], limit)
            if not text:
                return None
            normalized[field] = text

    identity_fields = ("session_id", "tool_use_id")
    evidence = value.get("evidence")
    normalized_evidence = {}
    for source, destination in ((value, normalized), (evidence, normalized_evidence)):
        if not isinstance(source, dict):
            continue
        for field in identity_fields:
            if field not in source:
                continue
            identity = source[field]
            if identity is None:
                destination[field] = None
                continue
            if (
                not isinstance(identity, str)
                or len(identity.encode("utf-16-le", errors="surrogatepass")) // 2 > 256
                or re.search(r"[\x00-\x1f\x7f-\x9f]", identity)
            ):
                return None
            destination[field] = identity
    if normalized_evidence:
        normalized["evidence"] = normalized_evidence
    return normalized


def _explanation_matches_request_ids(explanation: dict[str, Any], payload: dict[str, Any]) -> bool:
    """Reject explanation identities that do not describe this spawn request."""
    evidence = explanation.get("evidence")
    sources = (explanation, evidence) if isinstance(evidence, dict) else (explanation,)
    for field in ("session_id", "tool_use_id"):
        identities = [
            source[field] for source in sources if field in source and source[field] is not None
        ]
        if not identities:
            continue
        expected = payload.get(field)
        if (
            not isinstance(expected, str)
            or not expected
            or any(identity != expected for identity in identities)
        ):
            return False
    return True


def configured_router_name() -> str:
    """Return the environment-selected router, falling back to ``stable``."""
    return os.environ.get(ROUTER_NAME_ENV_VAR, "").strip() or ROUTER_NAME


def route_options_for(models: Iterable[str], harness: str) -> list[tuple[str, str]]:
    """Return one ``(model, harness)`` option per distinct model id, unchanged and in order."""
    return [(model, harness) for model in dict.fromkeys(models)]


def select_route(
    workspace: str,
    token: str,
    task: str,
    route_options: Iterable[tuple[str, str | None]],
    *,
    router_name: str,
    timeout: float = REQUEST_TIMEOUT_S,
) -> tuple[RoutingDecision | None, str | None]:
    """POST one ``routes:select`` request and return the router's pick.

    ``route_options`` are ``(model, harness)`` pairs sent as given. The gateway
    canonicalizes model names itself and returns the option it was sent, so a
    pick that was not offered is rejected. Returns ``(decision, error)``; a
    failed call yields ``(None, reason)`` so callers can fail open.
    """
    offered_routes = tuple(route_options)
    body = {
        "route_options": [
            {"model": model, "harness": harness} for model, harness in offered_routes
        ],
        "task": {"prompt": task},
        "route_selector": {"router_name": router_name},
    }
    request = urllib.request.Request(
        workspace.rstrip("/") + ROUTING_PATH,
        data=json.dumps(body).encode("utf-8"),
        headers={
            **ug_headers(),
            **workspace_headers(workspace),
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace").strip()
        except OSError:
            pass
        reason = f"router returned HTTP {exc.code}"
        if detail:
            reason = f"{reason}: {detail[:300]}"
        return None, reason
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        return None, f"router request failed: {exc}"

    model = _selected_model(payload)
    if model is None:
        return None, "router returned no model selection"
    if model not in {offered for offered, _ in offered_routes}:
        return None, f"router selected a model it was not offered: {model!r}"
    raw_rationale = payload.get("rationale")
    raw_rationale = raw_rationale if isinstance(raw_rationale, str) else ""
    rationale = format_rationale(payload["route_selection"][0], raw_rationale)
    params = payload["route_selection"][0].get("params")
    raw_explanation = params.get("routing_explanation") if isinstance(params, dict) else None
    explanation = _parse_routing_explanation(raw_explanation, model, offered_routes)
    if raw_explanation is not None and explanation is None:
        logging.getLogger(__name__).debug(
            "Discarded routing explanation: invalid schema or offered-model identities"
        )
    return (
        RoutingDecision(
            model=model,
            rationale=rationale,
            route_options=offered_routes,
            router_name=router_name,
            raw_rationale=raw_rationale if rationale != raw_rationale else None,
            explanation=explanation,
        ),
        None,
    )


def resolve_spawn_route(
    payload: dict[str, Any],
    *,
    is_spawn_agent: Callable[[Any], bool],
    decision_fn: Callable[[str], tuple[RoutingDecision | None, str | None]],
    default_task_label: str,
    model_id_mapper: Callable[[str], str],
) -> SpawnRoute | None:
    """Resolve one subagent-spawn payload to a routed model."""
    if not is_spawn_agent(payload.get("tool_name")):
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    # Derive the routing task from the first available plaintext field. The
    # harness-specific names are tried in order: `prompt`/`description` (Claude
    # Code's Agent tool), `message` (Codex's plaintext native spawn_agent assignment), then `task_name` /
    # `agent_name` (weaker labels for other payloads), then the generic default.
    task = next(
        (
            value
            for field in ("prompt", "description", "message", "task_name", "agent_name")
            if (value := _plaintext_string(tool_input.get(field))) is not None
        ),
        default_task_label,
    )
    decision, reason = decision_fn(task)
    if decision is None:
        logging.getLogger(__name__).debug(
            "Smart Router could not route subagent: %s", reason or "no routing decision"
        )
        return None
    if decision.explanation is not None and not _explanation_matches_request_ids(
        decision.explanation, payload
    ):
        logging.getLogger(__name__).debug(
            "Discarded routing explanation: session or tool-call identity does not match request"
        )
        decision = replace(decision, explanation=None)
    routed_model = model_id_mapper(decision.model)
    return SpawnRoute(tool_input, task, decision, routed_model)


def route_spawn_tool(
    payload: dict[str, Any],
    *,
    is_spawn_agent: Callable[[Any], bool],
    decision_fn: Callable[[str], tuple[RoutingDecision | None, str | None]],
    default_task_label: str,
    model_id_mapper: Callable[[str], str],
    notice_config: SubagentNoticeConfig,
    record_decision: Callable[[dict[str, Any], str, RoutingDecision, str], None] | None = None,
) -> dict[str, Any] | None:
    """Route one subagent-spawn tool call, rewriting its ``model`` input."""
    route = resolve_spawn_route(
        payload,
        is_spawn_agent=is_spawn_agent,
        decision_fn=decision_fn,
        default_task_label=default_task_label,
        model_id_mapper=model_id_mapper,
    )
    if route is None:
        return None
    if record_decision is not None:
        record_decision(payload, route.task, route.decision, route.routed_model)
    # Include the same routing notice in both hook response fields.
    routing_message = notice_config.message(
        route.decision,
        route.routed_model,
        route.tool_input,
    )
    output: dict[str, Any] = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "updatedInput": {**route.tool_input, "model": route.routed_model},
        "permissionDecisionReason": routing_message,
    }
    return {"systemMessage": routing_message, "hookSpecificOutput": output}


def record_session_start(canary_path: Path, payload: dict[str, Any]) -> None:
    """Write a canary proving the harness trusted and ran the routing hooks."""
    _write_json(
        canary_path,
        {
            "session_id": payload.get("session_id"),
            "model": payload.get("model"),
            "at": time.time(),
        },
    )


def record_subagent_start(
    decisions_path: Path, audit_path: Path, payload: dict[str, Any]
) -> dict[str, Any]:
    """Append the model the harness actually selected for a routed subagent.

    Reconciles against the pending routing decision for the session (if any) so
    the audit row records whether the launched model matched the router's pick.
    """
    actual_model = payload.get("model")
    decision = _pending_decision(
        decisions_path, audit_path, payload.get("session_id"), actual_model
    )
    record = {
        "agent_id": payload.get("agent_id"),
        "agent_type": payload.get("agent_type"),
        "model": actual_model,
        "session_id": payload.get("session_id"),
        "at": time.time(),
    }
    if decision is not None:
        # When the harness doesn't report the subagent's model (actual_model is
        # None), we can't verify the match — record None rather than a false
        # mismatch. The PreToolUse hook already injected the routed model, so
        # routing still worked; the reconciliation is observability, not enforcement.
        matches = None if actual_model is None else decision.get("requested_model") == actual_model
        record.update(
            {
                "decision_id": decision.get("decision_id"),
                "router_model": decision.get("router_model"),
                "requested_model": decision.get("requested_model"),
                "matches_router_decision": matches,
            }
        )
    _append_jsonl(audit_path, record)
    return record


def write_decision_record(
    decisions_path: Path,
    payload: dict[str, Any],
    task_name: str,
    decision: RoutingDecision,
    requested_model: str,
    *,
    include_task_text: bool = True,
) -> None:
    """Record a routing decision so a later SubagentStart can reconcile it."""
    tool_input = payload.get("tool_input")
    original_input = tool_input if isinstance(tool_input, dict) else {}
    task_fields = (
        {"task_name": task_name}
        if include_task_text
        else {
            "task_sha256": hashlib.sha256(task_name.encode("utf-8")).hexdigest(),
            "task_length": len(task_name),
        }
    )
    _append_jsonl(
        decisions_path,
        {
            "decision_id": uuid.uuid4().hex,
            "session_id": payload.get("session_id"),
            "tool_use_id": payload.get("tool_use_id"),
            "original_model": original_input.get("model"),
            "original_subagent_type": original_input.get("subagent_type"),
            **task_fields,
            "router_model": decision.model,
            "requested_model": requested_model,
            "route_options": [
                {"model": model, "harness": harness} for model, harness in decision.route_options
            ],
            "router_name": decision.router_name,
            **(
                {"explanation": decision.explanation}
                if include_task_text and decision.explanation is not None
                else {}
            ),
            # Persisted for diagnosis: an empty value means the gateway returned
            # no rationale (vs. a placement bug in how we surface it).
            "rationale": decision.rationale,
            "raw_rationale": decision.raw_rationale
            if decision.raw_rationale is not None
            else decision.rationale,
            "at": time.time(),
        },
    )


def clear_artifacts(paths: Iterable[Path]) -> None:
    """Remove ucode-owned routing canary/audit/decision files."""
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue


def _selected_model(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    selections = payload.get("route_selection")
    if not isinstance(selections, list) or not selections:
        return None
    selection = selections[0]
    if not isinstance(selection, dict):
        return None
    option = selection.get("route_option")
    if not isinstance(option, dict):
        return None
    model = option.get("model")
    return model if isinstance(model, str) and model else None


def _pending_decision(
    decisions_path: Path, audit_path: Path, session_id: Any, actual_model: Any
) -> dict[str, Any] | None:
    used = {
        record.get("decision_id") for record in _read_jsonl(audit_path) if record.get("decision_id")
    }
    pending = [
        decision
        for decision in _read_jsonl(decisions_path)
        if decision.get("session_id") == session_id and decision.get("decision_id") not in used
    ]
    return next(
        (decision for decision in pending if decision.get("requested_model") == actual_model),
        pending[0] if pending else None,
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")
    except OSError:
        return


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        return
