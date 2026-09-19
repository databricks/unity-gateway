"""Shared AI Gateway routing helpers for coding-agent sessions and subagents.

Both the Codex and Claude Code integrations route through the workspace's
configured router at ``/ai-gateway/routing/v1/routes:select``. The
harness-agnostic mechanics live here — the gateway call, the decision shape,
model-name normalization, and the canary/audit/decision bookkeeping. Each
harness module (``codex_routing`` / ``claude_routing``) supplies its own route
arms, spawn-tool detector, model-id translation, and artifact paths.
"""

from __future__ import annotations

import json
import os
import re
import textwrap
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROUTER_NAME = "task_v3"
ROUTER_NAME_ENV_VAR = "SMART_ROUTER_NAME"
ROUTING_PATH = "/ai-gateway/routing/v1/routes:select"
REQUEST_TIMEOUT_S = 30.0
SUBAGENT_ROUTING_DISCLAIMER = (
    "Spawned subagents are routed independently based on their own complexity."
)
ROUTING_BOX_WIDTH = 73
_ANTHROPIC_AIGW_MODEL_RE = re.compile(r"^anthropic-aigw-[0-9a-fA-F]{8}-(.+)$")
_FERNET_TOKEN_RE = re.compile(r"^gAAAAA[A-Za-z0-9_-]+={0,2}$")


def format_switch_message(model: str, reason: str | None) -> str:
    """Format the first-prompt routed-model notice."""
    reason = _display_reason(reason)
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
    reason = _display_reason(reason)
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


def _display_reason(reason: str | None) -> str | None:
    """Hide echoed requests while retaining any following availability explanation."""
    if not reason:
        return reason
    return re.sub(
        r'(?:^|\s+)Request:\s*(?:“.*”\.|".*"\.|.*)',
        "",
        reason,
        count=1,
        flags=re.DOTALL,
    ).strip()


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
    """One model selection returned by the AI Gateway router."""

    model: str
    raw_model: str
    rationale: str = ""

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
        message = decision.display_message(
            model_label=self.display_model(decision.model, routed_model),
            is_subagent=True,
            subagent_name=self.name(tool_input),
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


def normalize_model(model: str) -> str:
    """Strip provider prefixes so router arms and workspace ids compare equal.

    ``system.ai.claude-opus-4-8`` and ``databricks-claude-opus-4-8`` both
    normalize to ``claude-opus-4-8`` — the router's canonical arm vocabulary.
    """
    tail = model.rsplit("/", 1)[-1]
    for prefix in ("databricks-", "system.ai."):
        if tail.startswith(prefix):
            tail = tail[len(prefix) :]
            break
    return tail.lower()


def unwrap_anthropic_gateway_model(model: str) -> str:
    """Strip Claude's synthetic Anthropic gateway prefix from a model id."""
    if match := _ANTHROPIC_AIGW_MODEL_RE.fullmatch(model):
        return match.group(1)
    return model


def configured_router_name() -> str:
    """Return the environment-selected router, falling back to ``task_v3``."""
    return os.environ.get(ROUTER_NAME_ENV_VAR, "").strip() or ROUTER_NAME


def select_route(
    workspace: str,
    token: str,
    task: str,
    route_options: Iterable[tuple[str, str | None]],
    resolve: Callable[[str], str | None],
    *,
    router_name: str,
    timeout: float = REQUEST_TIMEOUT_S,
) -> tuple[RoutingDecision | None, str | None]:
    """POST one ``routes:select`` request and resolve the router's pick.

    ``route_options`` are ``(model, harness)`` pairs offered to the router (the
    frozen menu the caller's harness scenario requires). ``resolve`` maps the
    router's chosen arm back to a model id the workspace can serve, returning
    None when the arm is unservable. Returns ``(decision, error)``; a failed
    call yields ``(None, reason)`` so callers can fail open.
    """
    body = {
        "route_options": [{"model": model, "harness": harness} for model, harness in route_options],
        "task": {"prompt": task},
        "route_selector": {"router_name": router_name},
    }
    request = urllib.request.Request(
        workspace.rstrip("/") + ROUTING_PATH,
        data=json.dumps(body).encode("utf-8"),
        headers={
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

    raw_model = _selected_model(payload)
    if raw_model is None:
        return None, "router returned no model selection"
    model = resolve(raw_model)
    if model is None:
        return None, f"router selected unsupported model {raw_model!r}"
    rationale = payload.get("rationale") if isinstance(payload, dict) else None
    return (
        RoutingDecision(
            model=model,
            raw_model=raw_model,
            rationale=rationale if isinstance(rationale, str) else "",
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
    # Code's Agent tool), `message` (Codex's spawn_agent — encrypted at
    # send-time but readable here because the PreToolUse hook fires before
    # that), then `task_name` / `agent_name` (weaker labels), then the generic
    # default.
    task = next(
        (
            value
            for field in ("prompt", "description", "message", "task_name", "agent_name")
            if (value := _plaintext_string(tool_input.get(field))) is not None
        ),
        default_task_label,
    )
    decision, _ = decision_fn(task)
    if decision is None:
        return None
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
    skip_arms: dict[str, str] | None = None,
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
    if skip_arms and route.decision.raw_model in skip_arms:
        return {"systemMessage": skip_arms[route.decision.raw_model]}
    if record_decision is not None:
        record_decision(payload, route.task, route.decision, route.routed_model)
    # Include the same routing notice in both hook response fields.
    # Claude launches OSS models with an `anthropic-aigw-...` alias, but the notice
    # displays the shorter `system.ai...` model ID.
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
) -> None:
    """Record a routing decision so a later SubagentStart can reconcile it."""
    _append_jsonl(
        decisions_path,
        {
            "decision_id": uuid.uuid4().hex,
            "session_id": payload.get("session_id"),
            "task_name": task_name,
            "router_model": decision.raw_model,
            "requested_model": requested_model,
            # Persisted for diagnosis: an empty value means the gateway returned
            # no rationale (vs. a placement bug in how we surface it).
            "rationale": decision.rationale,
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
