"""Append-only, per-session records of subagent routing decisions."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import textwrap
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ucode.config_io import APP_DIR
from ucode.os_compatibility.file_lock_cross_os import acquire_exclusive_file_lock, release_file_lock
from ucode.smart_routing.routing import ROUTING_BOX_WIDTH, RoutingDecision, draw_box

SCHEMA_VERSION = 1
DECISION_TYPE = "decision"
ROUTE_VALIDATION_TYPE = "route_validation"
ROUTE_VALIDATION_FIELDS = (
    "router_name",
    "routing_unsupported_reason",
    "validation_failed",
    "validation_failure_reason",
    "eligible_route_options",
    "ineligible_route_options",
    "available_route_options",
    "validation_ms",
    "routing_disabled",
)
LOG_DIR = APP_DIR / "smart-router" / "sessions"
RETENTION_SECONDS = 30 * 24 * 3600
PREVIEW_CHARS = 160
TABLE_MAX_ROWS = 20
TABLE_TASK_COLUMN = 2
TABLE_MODEL_COLUMN = 3
TABLE_COLUMN_GAP = 2
TABLE_MIN_TASK_WIDTH = 12
ID_PREFIX_CHARS = 8
MAX_EPOCH_SECONDS = 8.64e12  # largest time a JavaScript Date accepts
PROMPT_PLACEHOLDER = "(prompt not recorded)"
MIN_ID_PREFIX_CHARS = 4
DETAIL_LABEL_WIDTH = 17
MODEL_PREFIXES = ("system.ai.", "databricks-")
ROUTE_VALIDATION_REASONS = {
    "insufficient_eligible_options": "At least two supported models are required. Please reach out to your workspace admin",
    "missing_required_tier_option": "Required models are missing from this list",
    "INCOMPLETE_REQUIRED_OPTIONS": "Required models are missing from this list",
    "harness_not_supported": "Harness not supported",
    "mixed_harnesses_not_supported": "Mixed harnesses not supported",
}
ROUTE_VALIDATION_GENERIC_REASON = "Smart Routing does not support this agent's model list."
MODEL_REASONS = {
    "model_not_supported": "Model source not supported",
    "model_not_in_recipe": "Not available in Smart Router",
    "excluded_by_policy": "Excluded by routing policy",
    "model_service_unresolved": "Model service unresolved",
    "model_provider_service_unresolved": "Model provider service unresolved",
    "harness_not_supported": "",
}
REASON_CODE = re.compile(r"[A-Za-z0-9_]+")


def session_log_path(harness: str, session_id: object) -> Path | None:
    if not isinstance(session_id, str) or not session_id:
        return None
    safe = (
        session_id
        if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id)
        else hashlib.sha256(session_id.encode("utf-8", errors="surrogatepass")).hexdigest()
    )
    return LOG_DIR / f"{harness}-{safe}.jsonl"


def append_decision(
    harness: str,
    payload: dict[str, Any],
    task_text: str,
    decision: RoutingDecision,
    requested_model: str,
    *,
    routing_ms: int,
) -> None:
    """Write a decision without allowing filesystem failures to block routing."""
    try:
        path = session_log_path(harness, payload.get("session_id"))
        if path is None:
            return
        preview = next((line.strip() for line in task_text.splitlines() if line.strip()), None)
        if preview is not None and len(preview) > PREVIEW_CHARS:
            preview = preview[:PREVIEW_CHARS] + "…"
        original = payload.get("tool_input")
        original = original if isinstance(original, dict) else {}
        record = {
            "v": SCHEMA_VERSION,
            "type": DECISION_TYPE,
            "decision_id": uuid.uuid4().hex,
            "at": time.time(),
            "harness": harness,
            "kind": "subagent",
            "session_id": payload["session_id"],
            "tool_use_id": payload.get("tool_use_id"),
            # Only a bounded first-line preview is stored; the full prompt text
            # stays out of ~/.ucode (the decisions file already holds it).
            "prompt": {
                "preview": preview,
                "sha256": hashlib.sha256(
                    task_text.encode("utf-8", errors="surrogatepass")
                ).hexdigest(),
                "length": len(task_text),
            },
            "original": {
                "model": original.get("model"),
                "subagent_type": original.get("subagent_type"),
            },
            "selected_model": decision.model,
            "requested_model": requested_model,
            "candidates": [
                {"model": model, "harness": candidate_harness}
                for model, candidate_harness in decision.route_options
            ],
            **({"explanation": decision.explanation} if decision.explanation is not None else {}),
            "router_name": decision.router_name,
            "routing_ms": routing_ms,
            "rationale": decision.rationale,
            "raw_rationale": decision.raw_rationale,
        }
        _append(path, record)
    except (OSError, TypeError, ValueError):
        pass


def _append(path: Path, record: dict[str, Any]) -> None:
    line = (json.dumps(record, ensure_ascii=True) + "\n").encode("utf-8")
    LOG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        fd = os.open(path, flags, 0o600)
        created = False
    with os.fdopen(fd, "ab", buffering=0) as log:
        if created:
            for old in LOG_DIR.glob("*.jsonl"):
                try:
                    if old != path and old.stat().st_mtime < time.time() - RETENTION_SECONDS:
                        old.unlink()
                except OSError:
                    pass
        acquire_exclusive_file_lock(log, timeout=0.1)
        try:
            os.write(log.fileno(), line)
        finally:
            release_file_lock(log)


def append_route_validation(harness: str, session_id: str, result: dict[str, Any]) -> None:
    """Record an unsupported or failed route validation; filesystem errors propagate to the caller."""
    path = session_log_path(harness, session_id)
    if path is None:
        raise ValueError("session id is required")
    _append(
        path,
        {
            "v": SCHEMA_VERSION,
            "type": ROUTE_VALIDATION_TYPE,
            "validation_id": uuid.uuid4().hex,
            "at": time.time(),
            "harness": harness,
            "session_id": session_id,
            **{key: result.get(key) for key in ROUTE_VALIDATION_FIELDS},
        },
    )


def read_decisions(harness: str, session_id: str) -> list[dict[str, Any]]:
    """Return a session's decision records in chronological file order."""
    return read_records(harness, session_id, DECISION_TYPE)


def read_history(harness: str, session_id: str) -> list[dict[str, Any]]:
    """Return a session's decision and route validation records in chronological file order."""
    return read_records(harness, session_id, (DECISION_TYPE, ROUTE_VALIDATION_TYPE))


def read_records(
    harness: str, session_id: str, record_type: str | tuple[str, ...]
) -> list[dict[str, Any]]:
    """Return a session's records of the given type or types in chronological file order."""
    record_types = (record_type,) if isinstance(record_type, str) else record_type
    path = session_log_path(harness, session_id)
    if path is None:
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        # A session without a log simply has no recorded decisions.
        return []
    # Other read failures (permissions, I/O) propagate: the caller must not
    # present an unreadable log as an empty history.
    records: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(record, dict):
            continue
        if record.get("v") != SCHEMA_VERSION or record.get("type") not in record_types:
            continue
        records.append(record)
    return records


def overview(record: dict[str, Any]) -> dict[str, Any]:
    """Return the summary fields a UI pane shows for one decision record."""
    prompt = record.get("prompt")
    preview = prompt.get("preview") if isinstance(prompt, dict) else None
    return {
        "decision_id": record.get("decision_id"),
        "at": record.get("at"),
        "kind": record.get("kind"),
        "preview": preview,
        "selected_model": record.get("selected_model"),
        "routing_ms": record.get("routing_ms"),
    }


def _age(at: object, now: float) -> str:
    if not isinstance(at, int | float) or not _is_number(at) or abs(at) > MAX_EPOCH_SECONDS:
        return "?"
    seconds = max(0, int(now - at))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _clean(text: str) -> str:
    """Replace control characters, so recorded text cannot drive the terminal."""
    return _CONTROL_CHARS.sub("\ufffd", text)


def _short_id(decision_id: object) -> str:
    return _clean(decision_id[:ID_PREFIX_CHARS]) if isinstance(decision_id, str) else "?"


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _short_model(model: object) -> str:
    if not isinstance(model, str):
        return "?"
    for prefix in MODEL_PREFIXES:
        model = model.removeprefix(prefix)
    return _clean(model)


def _is_number(value: object) -> bool:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # an integer too large for a float
        return False


def _is_listed(record: dict[str, Any]) -> bool:
    """Mirror the Claude logs pane's row filter so both harnesses list the same records."""
    summary = overview(record)
    decision_id = summary["decision_id"]
    preview = summary["preview"]
    return (
        isinstance(decision_id, str)
        and bool(decision_id)
        and _is_number(summary["at"])
        and abs(summary["at"]) <= MAX_EPOCH_SECONDS
        and (preview is None or isinstance(preview, str))
        and isinstance(summary["selected_model"], str)
        and _is_number(summary["routing_ms"])
    )


def _is_validation(record: dict[str, Any]) -> bool:
    return record.get("type") == ROUTE_VALIDATION_TYPE


def _is_validation_listed(record: dict[str, Any]) -> bool:
    validation_id = record.get("validation_id")
    return (
        isinstance(validation_id, str)
        and bool(validation_id)
        and _is_number(record.get("at"))
        and abs(record["at"]) <= MAX_EPOCH_SECONDS
        and _is_number(record.get("validation_ms"))
    )


def _validation_task(record: dict[str, Any]) -> str:
    if record.get("validation_failed") is True:
        return "Failed to validate smart routing."
    if record.get("routing_disabled") is False:
        return "Warning: unsupported model list."
    return "Failed to enable smart routing."


def _table_row(record: dict[str, Any], now: float) -> tuple[str, ...]:
    if _is_validation(record):
        return (
            _short_id(record["validation_id"]),
            _age(record["at"], now),
            _validation_task(record),
            "—",
            f"{round(record['validation_ms'])} ms",
        )
    summary = overview(record)
    return (
        _short_id(summary["decision_id"]),
        _age(summary["at"], now),
        _clean(" ".join((summary["preview"] or PROMPT_PLACEHOLDER).split())),
        _short_model(summary["selected_model"]),
        f"{round(summary['routing_ms'])} ms",
    )


def _fit_columns(rows: Sequence[tuple[str, ...]], headers: tuple[str, ...]) -> list[int]:
    """Column widths that fit the box: the task column shrinks first, then the model."""
    widths = [max(len(cell) for cell in column) for column in zip(headers, *rows, strict=True)]
    task, model = TABLE_TASK_COLUMN, TABLE_MODEL_COLUMN
    overflow = sum(widths) + TABLE_COLUMN_GAP * (len(widths) - 1) - ROUTING_BOX_WIDTH
    widths[task] = max(TABLE_MIN_TASK_WIDTH, widths[task] - max(overflow, 0))
    overflow = sum(widths) + TABLE_COLUMN_GAP * (len(widths) - 1) - ROUTING_BOX_WIDTH
    widths[model] = max(len(headers[model]), widths[model] - max(overflow, 0))
    return widths


def render_table(records: list[dict[str, Any]], *, now: float | None = None) -> str:
    """Render the most recent decisions and route validations as a boxed table for chat."""
    records = [
        record
        for record in records
        if (_is_validation_listed(record) if _is_validation(record) else _is_listed(record))
    ]
    if not records:
        return draw_box(["No routed subagents recorded this session yet."])
    now = time.time() if now is None else now
    shown = records[-TABLE_MAX_ROWS:]
    rows = [_table_row(record, now) for record in shown]
    decisions = sum(1 for record in records if not _is_validation(record))
    headers = ("ID", "WHEN", "TASK", "MODEL", "ROUTING")
    widths = _fit_columns(rows, headers)
    table = [
        (TABLE_COLUMN_GAP * " ").join(
            _truncate(cell, width).ljust(width) for cell, width in zip(row, widths, strict=True)
        )
        for row in (headers, *rows)
    ]
    return "\n".join(
        [
            f"{decisions} routed subagents this session (showing last {len(shown)})",
            "",
            draw_box(table),
            "Show one: $smart-router logs --id <ID>",
        ]
    )


def _record_id(record: dict[str, Any]) -> str | None:
    value = record.get("decision_id") or record.get("validation_id")
    return value if isinstance(value, str) else None


def find_decision(records: list[dict[str, Any]], query: str) -> dict[str, Any]:
    """Resolve an exact decision or validation id, else a unique prefix of at least four characters."""
    exact = next((r for r in records if _record_id(r) == query), None)
    if exact is not None:
        return exact
    if len(query) < MIN_ID_PREFIX_CHARS:
        raise LookupError(f"decision id must be at least {MIN_ID_PREFIX_CHARS} characters")
    matches = [r for r in records if (_record_id(r) or "").startswith(query)]
    if not matches:
        raise LookupError("decision not found")
    if len(matches) > 1:
        raise LookupError(f"decision id prefix {query!r} is ambiguous; use more characters")
    return matches[0]


def _is_object(value: object) -> bool:
    return isinstance(value, dict)


def _is_detail_record(record: dict[str, Any]) -> bool:
    """Mirror the Claude detail view's validation of a decision record."""
    prompt = record.get("prompt")
    original = record.get("original")
    candidates = record.get("candidates")
    return (
        (
            prompt is None
            or (_is_object(prompt) and isinstance(prompt.get("preview", 0), str | None))
        )
        and (
            original is None
            or (_is_object(original) and isinstance(original.get("subagent_type", 0), str | None))
        )
        and isinstance(record.get("selected_model"), str)
        and _is_number(record.get("routing_ms"))
        and isinstance(record.get("rationale", 0), str | None)
        and isinstance(candidates, list)
        and all(_is_object(c) and isinstance(c.get("model"), str) for c in candidates)
    )


def _labeled(label: str, value: str) -> list[str]:
    return textwrap.wrap(
        value,
        width=ROUTING_BOX_WIDTH,
        initial_indent=label.ljust(DETAIL_LABEL_WIDTH),
        subsequent_indent=" " * DETAIL_LABEL_WIDTH,
        break_on_hyphens=False,
    ) or [label]


def render_detail(record: dict[str, Any], *, now: float | None = None) -> str:
    """Render one decision like the Claude logs detail view."""
    if not _is_detail_record(record):
        raise ValueError("Could not load this routing decision.")
    now = time.time() if now is None else now
    prompt = record.get("prompt") or {}
    original = record.get("original") or {}
    preview = prompt.get("preview")
    subagent = original.get("subagent_type")
    if isinstance(subagent, str) and re.search(r"(^|:)ucode-route-", subagent):
        subagent = None  # names a model, not a task type, so it would contradict the selection
    selected = record["selected_model"]
    rationale = (record["rationale"] or "").strip() or "No rationale returned."
    lines = [
        f"Routing decision {_short_id(record.get('decision_id'))} · {_age(record.get('at'), now)}",
        "",
        *_labeled(
            "Prompt preview:",
            _clean(" ".join(preview.split())) if isinstance(preview, str) else PROMPT_PLACEHOLDER,
        ),
    ]
    if isinstance(subagent, str):
        lines += _labeled("Agent type:", _clean(subagent))
    lines += _labeled("Selected:", _short_model(selected))
    lines += _labeled("Routing time:", f"{record['routing_ms'] / 1000:.1f}s")
    lines += _labeled("Rationale:", _clean(" ".join(rationale.split())))
    if record["candidates"]:
        lines += ["", "Candidates"]
        for candidate in record["candidates"]:
            is_selected = candidate["model"] == selected
            suffix = "  (selected)" if is_selected else ""
            line = f"  {'●' if is_selected else '○'} {_short_model(candidate['model'])}{suffix}"
            lines.append(_truncate(line, ROUTING_BOX_WIDTH))
    return "\n".join([draw_box(lines), "Back to the list: $smart-router logs"])


def _validation_reason(record: dict[str, Any]) -> str:
    if record.get("validation_failed") is True:
        failure = record.get("validation_failure_reason")
        text = failure.strip() if isinstance(failure, str) else ""
        return text or "Smart Router couldn't validate this agent's model list."
    reason = record.get("routing_unsupported_reason")
    if not isinstance(reason, str):
        return ROUTE_VALIDATION_GENERIC_REASON
    if reason in ROUTE_VALIDATION_REASONS:
        return ROUTE_VALIDATION_REASONS[reason]
    text = reason.strip()
    return text if text and not REASON_CODE.fullmatch(text) else ROUTE_VALIDATION_GENERIC_REASON


def _model_reason(reason: object) -> str:
    return (
        MODEL_REASONS[reason]
        if isinstance(reason, str) and reason in MODEL_REASONS
        else "Not eligible for routing"
    )


def _is_option_list(value: object, *, with_reason: bool = False) -> bool:
    return isinstance(value, list) and all(
        _is_object(option)
        and isinstance(option.get("model"), str)
        and (not with_reason or isinstance(option.get("reason", 0), str | None))
        for option in value
    )


def render_validation_detail(record: dict[str, Any]) -> str:
    """Render one route validation like the Claude logs detail view."""
    if not (
        _is_number(record.get("validation_ms"))
        and _is_option_list(record.get("eligible_route_options"))
        and _is_option_list(record.get("ineligible_route_options"), with_reason=True)
        and _is_option_list(record.get("available_route_options"))
    ):
        raise ValueError("Could not load this route validation.")
    lines = [
        *_labeled("Reason:", _clean(" ".join(_validation_reason(record).split()))),
        *_labeled("Validation Time:", f"{record['validation_ms'] / 1000:.1f}s"),
    ]
    eligible = record["eligible_route_options"]
    ineligible = record["ineligible_route_options"]
    if eligible or ineligible:
        lines += ["", "Models"]
        lines += [f"✓ {_short_model(option['model'])}" for option in eligible]
        for option in ineligible:
            reason = _model_reason(option.get("reason"))
            suffix = f"  {reason}" if reason else ""
            lines.append(f"✗ {_short_model(option['model'])}{suffix}")
    if record["available_route_options"]:
        lines += ["", "Supported models"]
        lines += [
            f"  {_short_model(option['model'])}" for option in record["available_route_options"]
        ]
    lines = [_truncate(_clean(line), ROUTING_BOX_WIDTH) for line in lines]
    return "\n".join([draw_box(lines), "Back to the list: $smart-router logs"])


def clear_logs() -> None:
    """Remove the session log directory. Best effort, like clear_artifacts."""
    shutil.rmtree(LOG_DIR, ignore_errors=True)
