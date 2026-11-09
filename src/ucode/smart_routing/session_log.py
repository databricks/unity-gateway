"""Append-only, per-session records of subagent routing decisions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from ucode.config_io import APP_DIR
from ucode.os_compatibility.file_lock_cross_os import acquire_exclusive_file_lock, release_file_lock
from ucode.smart_routing.routing import RoutingDecision

SCHEMA_VERSION = 1
LOG_DIR = APP_DIR / "smart-router" / "sessions"
RETENTION_SECONDS = 30 * 24 * 3600
PREVIEW_CHARS = 160


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
            "type": "decision",
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
            "router_name": decision.router_name,
            "routing_ms": routing_ms,
            "rationale": decision.rationale,
            "raw_rationale": decision.raw_rationale,
        }
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
    except (OSError, TypeError, ValueError):
        pass


def read_decisions(harness: str, session_id: str) -> list[dict[str, Any]]:
    """Return a session's decision records in chronological file order."""
    path = session_log_path(harness, session_id)
    if path is None:
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("v") != SCHEMA_VERSION or record.get("type") != "decision":
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


def clear_logs() -> None:
    """Remove the session log directory. Best effort, like clear_artifacts."""
    shutil.rmtree(LOG_DIR, ignore_errors=True)
