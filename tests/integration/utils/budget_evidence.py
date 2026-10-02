"""Evidence helpers for the live budget-defaults journeys."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .evidence import agent_sessions, is_child_session

_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_BUDGET_LINE = re.compile(
    r"\$(?P<spend>[0-9][0-9,]*(?:\.[0-9]+)?)\s*/\s*"
    r"\$(?P<threshold>[0-9][0-9,]*(?:\.[0-9]+)?)\s+"
    r"(?P<percent>[0-9]+)%\s+used",
    re.MULTILINE,
)
_METER_LINE = re.compile(r"(?m)^\s*[│|]?\s*(?P<filled>█+)(?P<empty>░*)\s*[│|]?\s*$")
_USAGE_LINE = re.compile(
    r"Budget spend:\s*\$(?P<spend>[0-9][0-9,]*(?:\.[0-9]+)?)\s+of\s+"
    r"\$(?P<threshold>[0-9][0-9,]*(?:\.[0-9]+)?)\s+\((?P<percent>[0-9]+)%\)",
    re.MULTILINE,
)
_USAGE_METER_LINE = re.compile(r"(?m)^\s*\[(?P<filled>█+)(?P<empty>░*)\]\s*$")
METER_WIDTH = 28


@dataclass(frozen=True)
class BudgetPanelEvidence:
    """The user-visible values rendered by ``ug``'s Rich budget panel."""

    spend: Decimal
    threshold: Decimal
    percent: int
    filled: int
    empty: int
    text: str

    @property
    def meter_width(self) -> int:
        return self.filled + self.empty


@dataclass(frozen=True)
class UsageSummaryEvidence:
    """The values rendered by the public ``ug usage`` command."""

    spend: Decimal
    threshold: Decimal
    percent: int
    filled: int
    empty: int
    text: str

    @property
    def meter_width(self) -> int:
        return self.filled + self.empty


def clean_terminal_text(value: str) -> str:
    """Remove terminal control sequences while preserving visible line breaks."""

    return _ANSI_ESCAPE.sub("", value).replace("\r", "")


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise AssertionError(f"Budget recommendation has no {field}: {value!r}")
    try:
        result = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError) as exc:
        raise AssertionError(f"Budget recommendation has invalid {field}: {value!r}") from exc
    if not result.is_finite():
        raise AssertionError(f"Budget recommendation has non-finite {field}: {value!r}")
    return result


def recommendation_value(recommendation: dict[str, Any], field: str) -> Any:
    """Read one field from the raw ``recommendModel`` wire response."""

    raw_name = {
        "agent": "recommended_agent",
        "model": "recommended_model",
        "spend": "current_spend",
        "threshold": "effective_threshold",
    }.get(field)
    if raw_name is None:
        raise AssertionError(f"Unknown recommendation field: {field}")
    return recommendation.get(raw_name)


def recommendation_spend(recommendation: dict[str, Any]) -> Decimal:
    value = recommendation_value(recommendation, "spend")
    return _decimal(value, "current_spend")


def recommendation_threshold(recommendation: dict[str, Any]) -> Decimal:
    value = recommendation_value(recommendation, "threshold")
    return _decimal(value, "effective_threshold")


def usage_percent(spend: Decimal, threshold: Decimal) -> int:
    """Match the product's float-based ``budget_usage_percent`` exactly."""

    if threshold <= 0:
        return 0
    return max(int(((float(spend) / float(threshold)) * 100) + 0.5), 0)


def meter_filled(percent: int) -> int:
    """Match the 28-cell Rich meter used by the product."""

    capped = min(max(percent, 0), 100)
    return min(max((capped * METER_WIDTH + 50) // 100, 0), METER_WIDTH)


def _display_money(value: Decimal) -> Decimal:
    # The product converts API decimals to float before formatting with ``:.2f``.
    # Keep the boundary math Decimal-based, but mirror that final presentation step
    # exactly for fractional-cent thresholds.
    return Decimal(format(float(value), ",.2f").replace(",", ""))


def _usage_display_money(value: Decimal) -> Decimal:
    # ``ug usage`` uses format_usd(), which rounds Decimal values half-up.
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def parse_budget_panel(text: str) -> BudgetPanelEvidence:
    """Parse the last rendered budget panel from a native PTY transcript."""

    cleaned = clean_terminal_text(text)
    matches = list(_BUDGET_LINE.finditer(cleaned))
    assert matches, "The launch transcript did not contain a rendered budget spend line."
    match = matches[-1]
    meter_matches = list(_METER_LINE.finditer(cleaned[match.end() :]))
    if not meter_matches:
        # A Rich panel can be followed by the TUI's clear-screen sequence.  Search
        # the complete cleaned transcript as a fallback, still requiring the native
        # block meter rather than inferring it from the percentage.
        meter_matches = list(_METER_LINE.finditer(cleaned))
    assert meter_matches, "The launch transcript did not contain the rendered budget meter."
    meter_match = meter_matches[-1]
    filled = len(meter_match.group("filled"))
    empty = len(meter_match.group("empty"))
    assert filled + empty == METER_WIDTH, (
        f"Unexpected budget meter width {filled + empty}; transcript was:\n{cleaned}"
    )
    return BudgetPanelEvidence(
        spend=Decimal(match.group("spend").replace(",", "")),
        threshold=Decimal(match.group("threshold").replace(",", "")),
        percent=int(match.group("percent")),
        filled=filled,
        empty=empty,
        text=cleaned,
    )


def usage_summary_percent(spend: Decimal, threshold: Decimal) -> int:
    """Match the percent formatting in ``render_budget_summary``."""

    if threshold <= 0:
        return 0
    return int(format(float(spend / threshold), ".0%").removesuffix("%"))


def parse_usage_summary(text: str) -> UsageSummaryEvidence:
    """Parse the last ``ug usage`` summary and its 30-cell meter."""

    cleaned = clean_terminal_text(text)
    matches = list(_USAGE_LINE.finditer(cleaned))
    assert matches, "The usage command did not contain a budget summary line."
    match = matches[-1]
    meter_matches = list(_USAGE_METER_LINE.finditer(cleaned[match.end() :]))
    if not meter_matches:
        meter_matches = list(_USAGE_METER_LINE.finditer(cleaned))
    assert meter_matches, "The usage command did not contain its budget meter."
    meter = meter_matches[-1]
    filled = len(meter.group("filled"))
    empty = len(meter.group("empty"))
    assert filled + empty == 30, f"Unexpected usage meter width {filled + empty}:\n{cleaned}"
    return UsageSummaryEvidence(
        spend=Decimal(match.group("spend").replace(",", "")),
        threshold=Decimal(match.group("threshold").replace(",", "")),
        percent=int(match.group("percent")),
        filled=filled,
        empty=empty,
        text=cleaned,
    )


def assert_usage_summary(
    text: str, recommendation: dict[str, Any], label: str
) -> UsageSummaryEvidence:
    """Compare ``ug usage``'s live spend and limit with the raw recommendation."""

    summary = parse_usage_summary(text)
    spend = recommendation_spend(recommendation)
    threshold = recommendation_threshold(recommendation)
    assert summary.spend == _usage_display_money(spend), (
        f"{label}: usage spend {summary.spend} != raw spend {spend}; transcript:\n{summary.text}"
    )
    assert summary.threshold == _usage_display_money(threshold), (
        f"{label}: usage threshold {summary.threshold} != raw threshold {threshold}; "
        f"transcript:\n{summary.text}"
    )
    expected_percent = usage_summary_percent(spend, threshold)
    assert summary.percent == expected_percent, (
        f"{label}: usage percent {summary.percent} != {expected_percent}; transcript:\n{summary.text}"
    )
    expected_filled = max(int(float(spend / threshold) * 30), 1) if threshold > 0 else 0
    expected_filled = min(expected_filled, 30)
    assert summary.filled == expected_filled, (
        f"{label}: usage meter has {summary.filled} filled cells != {expected_filled}; "
        f"transcript:\n{summary.text}"
    )
    return summary


def assert_budget_panel(
    text: str, recommendation: dict[str, Any], label: str
) -> BudgetPanelEvidence:
    """Compare raw recommendation dollars, percentage, and meter with the UI."""

    panel = parse_budget_panel(text)
    spend = recommendation_spend(recommendation)
    threshold = recommendation_threshold(recommendation)
    assert panel.spend == _display_money(spend), (
        f"{label}: rendered spend {panel.spend} != raw spend {spend}; transcript:\n{panel.text}"
    )
    assert panel.threshold == _display_money(threshold), (
        f"{label}: rendered threshold {panel.threshold} != raw threshold {threshold}; "
        f"transcript:\n{panel.text}"
    )
    expected_percent = usage_percent(spend, threshold)
    assert panel.percent == expected_percent, (
        f"{label}: rendered percent {panel.percent} != Decimal-derived {expected_percent}; "
        f"raw spend={spend}, threshold={threshold}; transcript:\n{panel.text}"
    )
    expected_filled = meter_filled(expected_percent)
    assert panel.filled == expected_filled, (
        f"{label}: rendered meter has {panel.filled} filled cells != {expected_filled}; "
        f"transcript:\n{panel.text}"
    )
    return panel


def _agent_display(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return {
        "codex": "Codex",
        "CODING_AGENT_CODEX": "Codex",
        "claude": "Claude Code",
        "CODING_AGENT_CLAUDE_CODE": "Claude Code",
    }.get(value, value)


def assert_recommendation_visible(
    text: str,
    recommendation: dict[str, Any],
    label: str,
    *,
    expected_agent: str | None = None,
    expected_model: str | None = None,
) -> None:
    """Require the panel's recommendation sentence and its raw model identity."""

    cleaned = clean_terminal_text(text)
    assert "You've used" in cleaned and "Recommended" in cleaned, (
        f"{label}: budget recommendation sentence was not rendered:\n{cleaned}"
    )
    agent = _agent_display(expected_agent or recommendation_value(recommendation, "agent"))
    model = expected_model or recommendation_value(recommendation, "model")
    if agent:
        assert f"Recommended agent is {agent}" in cleaned, (
            f"{label}: expected recommended agent {agent!r}; transcript:\n{cleaned}"
        )
    if model:
        assert str(model) in cleaned, (
            f"{label}: expected recommended model {model!r}; transcript:\n{cleaned}"
        )


def _claude_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        part["text"]
        for part in content
        if isinstance(part, dict)
        and part.get("type") == "text"
        and isinstance(part.get("text"), str)
    )


def _claude_task_evidence(session, task_value: str) -> list[dict[str, Any]]:
    matches = []
    for path, records in agent_sessions(session, "claude").items():
        if is_child_session("claude", path, records):
            continue
        for index, record in enumerate(records):
            if record.get("type") != "assistant":
                continue
            message = record.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            answer = _claude_text(message)
            if task_value not in answer:
                continue
            model = message.get("model")
            assert isinstance(model, str) and model, (
                f"Claude answer for {task_value} has no model: {path}:{index}"
            )
            matches.append({"path": path, "record_index": index, "model": model, "answer": answer})
    return matches


def _codex_task_evidence(session, task_value: str) -> list[dict[str, Any]]:
    matches = []
    for path, records in agent_sessions(session, "codex").items():
        if is_child_session("codex", path, records):
            continue
        contexts = []
        for index, record in enumerate(records):
            payload = record.get("payload")
            if record.get("type") != "turn_context" or not isinstance(payload, dict):
                continue
            model = payload.get("model")
            if isinstance(model, str) and model:
                contexts.append((index, payload.get("turn_id"), model))

        for index, record in enumerate(records):
            payload = record.get("payload")
            if (
                record.get("type") != "event_msg"
                or not isinstance(payload, dict)
                or payload.get("type") != "task_complete"
            ):
                continue
            answer = payload.get("last_agent_message")
            if not isinstance(answer, str) or task_value not in answer:
                continue
            turn_id = payload.get("turn_id")
            assert isinstance(turn_id, str) and turn_id, (
                f"Codex task completion for {task_value} has no preceding model context "
                f"because turn_id is missing: {path}:{index}"
            )
            candidates = [row for row in contexts if row[0] <= index and row[1] == turn_id]
            assert candidates, (
                f"Codex task completion for {task_value} has no preceding model context: "
                f"{path}:{index}"
            )
            context_index, _, model = candidates[-1]
            matches.append(
                {
                    "path": path,
                    "record_index": index,
                    "turn_context_index": context_index,
                    "turn_id": turn_id,
                    "model": model,
                    "answer": answer,
                }
            )
    return matches


def _comparable_model(agent: str, model: str) -> str:
    model = model.removesuffix("[1m]").removesuffix("[200k]")
    if agent == "claude":
        return model.removeprefix("system.ai.")
    if agent == "codex":
        bare = model.removeprefix("system.ai.")
        match = re.fullmatch(r"gpt-(\d+)-(\d+)(-.+)?", bare)
        if match:
            major, minor, suffix = match.groups()
            return f"gpt-{major}.{minor}{suffix or ''}"
        return bare
    raise AssertionError(f"Unknown native agent: {agent}")


def assert_task_model(session, agent: str, task, expected_model: str, label: str) -> str:
    """Bind this launch's completed native answer to its preceding request model."""

    if agent == "claude":
        observed = _claude_task_evidence(session, task.value)
    elif agent == "codex":
        observed = _codex_task_evidence(session, task.value)
    else:
        raise AssertionError(f"Unknown native agent: {agent}")

    session.record(f"{label}-task-model.json", {"agent": agent, "requests": observed})
    assert observed, f"{label}: completed {agent} task exposed no native request model"
    models = {_comparable_model(agent, row["model"]) for row in observed}
    expected = _comparable_model(agent, expected_model)
    assert models == {expected}, (
        f"{label}: completed {agent} task used models {sorted(models)!r}, expected {expected!r}"
    )
    return observed[-1]["model"]


def capture_process_tree(terminal, *, max_depth: int = 12) -> dict[str, Any]:
    """Capture command/executable metadata for a live PTY child and its descendants.

    Only command metadata is collected.  The helper never reads process environments,
    command output, or other fields that could contain bearer tokens.
    """

    root_pid = int(terminal.child.pid)
    rows: list[dict[str, Any]] = []
    ps = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,command="],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    by_pid: dict[int, dict[str, Any]] = {}
    for line in ps.stdout.splitlines():
        match = re.match(r"\s*(\d+)\s+(\d+)\s+(.*)$", line)
        if not match:
            continue
        pid, ppid, command = int(match.group(1)), int(match.group(2)), match.group(3).strip()
        argv = command.split()
        row = {
            "pid": pid,
            "ppid": ppid,
            "argv": argv,
            "command": command,
            "executable": argv[0] if argv else "",
        }
        by_pid[pid] = row

    # Include the PTY child and descendants.  The launcher normally execs into the
    # native process, but npm wrappers and agent helpers can create a child.  Do not
    # collect ancestors: a parent command may contain unrelated test text or secrets.
    descendants: set[int] = {root_pid}
    for _ in range(max_depth):
        children = {row["pid"] for row in by_pid.values() if row["ppid"] in descendants}
        new = children - descendants
        if not new:
            break
        descendants.update(new)
    selected = set(descendants)
    rows.extend(by_pid[pid] for pid in sorted(selected) if pid in by_pid)
    # Linux exposes the real executable separately from ps's command line.  Keep
    # this best-effort and portable: macOS still gets the command's executable path.
    for row in rows:
        proc_exe = Path(f"/proc/{row['pid']}/exe")
        try:
            row["executable_path"] = os.readlink(proc_exe)
        except OSError:
            row["executable_path"] = row["executable"]
    return {"root_pid": root_pid, "descendant_pids": sorted(descendants), "nodes": rows}


def assert_native_process_tree(
    tree: dict[str, Any], agent: str, label: str, session=None
) -> dict[str, Any]:
    """Require a native Claude/Codex command in the captured PTY process tree."""

    expected = agent.lower()
    nodes = tree.get("nodes")
    assert isinstance(nodes, list) and nodes, f"{label}: process tree is empty: {tree!r}"
    descendant_pids = set(tree.get("descendant_pids") or [tree.get("root_pid")])

    package_path = {
        "claude": "/node_modules/@anthropic-ai/claude-code/",
        "codex": "/node_modules/@openai/codex/",
    }[expected]

    def is_native_command(row: dict[str, Any]) -> bool:
        command = str(row.get("command", "")).lower()
        executable = Path(str(row.get("executable", ""))).name.lower()
        executable_path = str(row.get("executable_path", "")).replace("\\", "/").lower()
        if executable in {expected, expected + ".exe", expected + ".cmd"}:
            return True
        if Path(executable_path).name in {expected, expected + ".exe", expected + ".cmd"}:
            return True
        if package_path in executable_path:
            return True
        for index, argument in enumerate(row.get("argv", [])):
            value = str(argument).replace("\\", "/").lower()
            basename = Path(value).name
            if index == 0 and basename in {
                expected,
                expected + ".js",
                expected + ".mjs",
                expected + ".cjs",
            }:
                return True
            # npm launches commonly invoke node with a package path rather than
            # using the native script as argv[0].  Match only the published package
            # for the selected agent so ``ug claude`` itself cannot satisfy this
            # check by its argument or by an unrelated ``/bin/`` path.
            if package_path in value:
                return True
        return package_path in command

    candidates = [
        row for row in nodes if row.get("pid") in descendant_pids if is_native_command(row)
    ]
    assert candidates, f"{label}: no native {agent} command in process tree: {tree!r}"
    assert all(row.get("pid") and row.get("executable_path") for row in candidates), candidates
    tree["native_candidates"] = candidates
    if session is not None:
        session.record(f"{label}-process-tree.json", tree)
    return tree
