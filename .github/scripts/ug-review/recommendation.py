#!/usr/bin/env python3
"""Derive an explicit approve/reject recommendation from the Open Code Review result.

The script reads the raw OCR result JSON, asks the gateway model to emit a
machine-readable ``RECOMMENDATION: APPROVE|REJECT`` marker with reasons, and
exports the parsed outcome as a step output plus a markdown body for the
workflow to post on the pull request. A complete, warning-free review with no
findings approves without a model call. A missing or unparseable marker, a
failed model request, or an incomplete review becomes ``recommendation=none``:
no label changes, and the posted note says why. The recommendation is advisory;
the workflow keeps the ``ready-to-approve`` label in sync with it.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

RESULT_PATH = "/tmp/ocr-result.json"
BODY_PATH = "/tmp/ug-recommendation-body.md"
DEFAULT_MODEL = "system.ai.claude-opus-5-5"
MAX_PROMPT_FINDINGS = 8
MAX_FINDING_CHARS = 500
MAX_REASON_CHARS = 300
MAX_REASONS = 8

# The marker must sit on its own line so prose mentioning the word cannot parse.
MARKER_RE = re.compile(r"(?mi)^\s*RECOMMENDATION:[ \t]+(APPROVE|REJECT)\s*$")
REASONS_RE = re.compile(r"(?mi)^\s*REASONS:\s*$")
BULLET_RE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+")


class RecommendationError(RuntimeError):
    """The recommendation request or the OCR result cannot be used."""


def parse_recommendation(text: str | None) -> str | None:
    """Return 'approve' or 'reject' from the marker line, else None."""
    match = MARKER_RE.search(text or "")
    return match.group(1).lower() if match else None


def _strip_bullet(line: str) -> str:
    stripped = line.strip()
    while True:
        without_bullet = BULLET_RE.sub("", stripped, count=1)
        if without_bullet == stripped:
            return stripped
        stripped = without_bullet.strip()


def extract_reasons(text: str | None, marker: str | None) -> list[str]:
    """Return the reason bullets after REASONS:, or fallback lines."""
    body = text or ""
    reasons: list[str] = []
    reasons_match = REASONS_RE.search(body)
    if reasons_match:
        for line in body[reasons_match.end() :].splitlines():
            stripped = line.strip()
            if not stripped:
                if reasons:
                    break
                continue
            if not BULLET_RE.match(line):
                break
            reasons.append(_strip_bullet(line))
    if not reasons:
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped or MARKER_RE.match(line) or REASONS_RE.match(line):
                continue
            reasons.append(_strip_bullet(line))
    reasons = [reason[:MAX_REASON_CHARS] for reason in reasons[:MAX_REASONS]]
    if marker and not reasons:
        reasons.append(
            "The reviewer returned the marker without reasons; see the inline findings."
        )
    return reasons


def finding_briefs(comments: list[Any]) -> list[dict[str, Any]]:
    """Shape OCR comments into compact, bounded briefs for the model prompt."""
    briefs: list[dict[str, Any]] = []
    for comment in comments[:MAX_PROMPT_FINDINGS]:
        if not isinstance(comment, dict):
            continue
        content = str(comment.get("content") or "").strip()
        briefs.append(
            {
                "path": str(comment.get("path") or "?"),
                "lines": f"{comment.get('start_line')}-{comment.get('end_line')}",
                "severity": str(comment.get("severity") or "unknown"),
                "category": str(comment.get("category") or "unknown"),
                "content": content[:MAX_FINDING_CHARS],
            }
        )
    return briefs


def severity_counts(briefs: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for brief in briefs:
        counts[brief["severity"]] = counts.get(brief["severity"], 0) + 1
    return ", ".join(f"{name}: {count}" for name, count in sorted(counts.items())) or "none"


def build_prompt(briefs: list[dict[str, Any]]) -> str:
    findings = json.dumps(briefs, indent=2)
    return (
        "These are the findings Open Code Review reported on this pull request's "
        "changed lines:\n\n"
        f"{findings}\n\n"
        "Decide whether the pull request should be approved or rejected. Reject when "
        "any finding is a blocker or a major issue: data loss, credential or "
        "cross-workspace leakage, broken existing workflows, incorrect configuration "
        "ownership, a reproducible behavioral bug, an unsafe rollout assumption, or "
        "missing necessary regression coverage. Critical or high severities usually "
        "indicate this. Approve when the remaining findings are minor, optional "
        "follow-ups, or style notes. Respond with exactly:\n\n"
        "RECOMMENDATION: APPROVE\n"
        "REASONS:\n"
        "- <one concise reason per line>\n\n"
        "or RECOMMENDATION: REJECT with its reasons. List the reasons in both cases, "
        "one bullet each, referencing the findings."
    )


def request_recommendation(
    host: str, token: str, model: str, briefs: list[dict[str, Any]]
) -> str:
    """Ask the gateway model for the recommendation marker and return its text."""
    payload = {
        "model": model,
        "max_tokens": 2048,
        "messages": [{"role": "user", "content": build_prompt(briefs)}],
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "low"},
    }
    request = urllib.request.Request(
        f"{host.rstrip('/')}/ai-gateway/anthropic/v1/messages",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            data = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RecommendationError(f"gateway recommendation request failed: {exc}") from exc
    blocks = data.get("content") if isinstance(data, dict) else None
    text = "".join(
        block.get("text", "")
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    )
    if not text.strip():
        raise RecommendationError("gateway recommendation request returned no text")
    return text


def build_body(
    recommendation: str | None,
    reasons: list[str],
    briefs: list[dict[str, Any]],
    note: str,
    run_id: str,
    files_reviewed: int | None,
) -> str:
    """Render the markdown comment body for the derived recommendation."""
    lines = ["## UG review recommendation", ""]
    if recommendation == "approve":
        lines.append("**RECOMMENDATION: APPROVE**")
    elif recommendation == "reject":
        lines.append("**RECOMMENDATION: REJECT** (request changes)")
    else:
        lines.append(f"**No recommendation** — {note}")
        lines.append("")
        lines.append("No label was changed.")
    if recommendation in ("approve", "reject"):
        lines += ["", "Reasons:"]
        lines += [f"- {reason}" for reason in reasons]
        if briefs:
            lines += [
                "",
                f"Findings considered: {len(briefs)}"
                f" (severities: {severity_counts(briefs)}); see the inline review comments.",
            ]
        elif files_reviewed is not None:
            lines += ["", f"Complete review of {files_reviewed} files; no findings."]
        lines += [
            "",
            "Advisory only: this recommendation never approves or merges the pull"
            " request. The `ready-to-approve` label tracks it.",
        ]
    lines += ["", f"<!-- ug-review-recommendation-run:{run_id} -->"]
    return "\n".join(lines) + "\n"


def _emit(output_path: Path, recommendation: str, body: str, body_path: Path) -> None:
    with output_path.open("a", encoding="utf-8") as output:
        output.write(f"recommendation={recommendation}\n")
    body_path.write_text(body, encoding="utf-8")


def derive(
    result: dict[str, Any],
    request: Callable[[str, str, str, list[dict[str, Any]]], str],
    host: str,
    token: str,
    model: str,
) -> tuple[str | None, list[str], list[dict[str, Any]], str]:
    """Return (recommendation, reasons, briefs, note) for one OCR result."""
    if result.get("status") != "complete":
        return None, [], [], "the review did not complete, so there is nothing to judge"
    comments = result.get("comments")
    comments = comments if isinstance(comments, list) else []
    briefs = finding_briefs(comments)
    warnings = result.get("warnings")
    summary = result.get("summary") or {}
    files_reviewed = summary.get("files_reviewed") if isinstance(summary, dict) else None
    if not briefs:
        if warnings:
            return (
                None,
                [],
                [],
                "the review reported warnings and may be incomplete, so a clean verdict "
                "would be unsafe",
            )
        return (
            "approve",
            ["The review completed with no findings on the changed lines."],
            [],
            "",
        )
    text = request(host, token, model, briefs)
    marker = parse_recommendation(text)
    if marker is None:
        return (
            None,
            [],
            briefs,
            "the reviewer did not return a parseable `RECOMMENDATION: APPROVE|REJECT` "
            "marker",
        )
    return marker, extract_reasons(text, marker), briefs, ""


def main() -> int:
    run_id = os.environ.get("RECOMMENDATION_RUN", "unknown-run")
    result_path = Path(os.environ.get("OCR_RESULT_PATH", RESULT_PATH))
    body_path = Path(os.environ.get("BODY_PATH", BODY_PATH))
    output_path = Path(os.environ.get("GITHUB_OUTPUT", os.devnull))
    recommendation: str | None = None
    reasons: list[str] = []
    briefs: list[dict[str, Any]] = []
    note = ""
    files_reviewed: int | None = None
    try:
        raw = result_path.read_text(encoding="utf-8")
        result = json.loads(raw) if raw.strip() else {}
        if not isinstance(result, dict):
            raise RecommendationError("OCR result is not a JSON object")
        summary = result.get("summary") or {}
        if isinstance(summary, dict):
            files_reviewed = summary.get("files_reviewed")
        token = os.environ.get("GATEWAY_TOKEN", "").strip()
        host = os.environ.get("DATABRICKS_HOST", "").strip()
        model = os.environ.get("GATEWAY_MODEL", "").strip() or DEFAULT_MODEL
        if result.get("status") == "complete" and (not host or not token):
            raise RecommendationError("DATABRICKS_HOST or GATEWAY_TOKEN is missing")

        recommendation, reasons, briefs, note = derive(
            result, request_recommendation, host, token, model
        )
    except (RecommendationError, OSError, ValueError) as exc:
        note = f"the recommendation could not be derived ({exc})"
        recommendation = None
    if recommendation is None and note:
        print(f"::warning::{note}", flush=True)
    body = build_body(recommendation, reasons, briefs, note, run_id, files_reviewed)
    _emit(output_path, recommendation or "none", body, body_path)
    print(f"recommendation: {recommendation or 'none'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
