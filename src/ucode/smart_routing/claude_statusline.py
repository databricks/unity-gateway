"""Claude Code statusline showing an estimate of what smart routing saved this session.

``v2.launch_claude`` installs this as the session's ``statusLine`` command, wrapping any statusline
the user already had. The estimate assumes every token the session used, main agent and subagents
alike, would otherwise have run on the baseline main-agent model::

    saved = sum(tokens * price(baseline)) - sum(tokens * price(model that served them))

summed over every assistant response in the main and subagent transcripts, per token class. The
baseline is the main model the user chose; under first-prompt routing, where the router also picks
the main model, it is the model the session started on, before routing switched it. A negative
result (routing chose pricier models) is shown as a cost increase.

Claude Code debounces refreshes and cancels a run that is still going when the next one starts,
so this module stays stdlib-only (ug's CLI imports take about a second) and reads transcripts
incrementally, resuming from offsets kept in a per-session state file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sys
import tempfile
import time
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ucode.smart_routing.pricing import (
    ModelPrice,
    TokenUsage,
    model_key,
    read_price_cache,
    token_cost,
)

MODULE = "ucode.smart_routing.claude_statusline"
STATE_DIRNAME = "claude-savings"
STATE_RETENTION_SECONDS = 7 * 24 * 60 * 60
_STATE_VERSION = 1
# statusLine options that shape how the row renders rather than what it runs.
_PRESERVED_STATUS_LINE_KEYS = ("padding", "refreshInterval", "hideVimModeIndicator")
_SAFE_SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_CENT = Decimal("0.01")


def effective_status_line(
    settings: dict, *, user_settings_path: Path, project_dir: Path
) -> dict | None:
    """The statusLine Claude Code would run without ug's, or None.

    ``settings`` is the per-launch ``--settings`` document (caller settings merged with ug's), which
    outranks project-local, project, and user settings. Managed settings outrank ug's statusline as
    well, so there is nothing to wrap there: Claude Code shows the managed statusline instead.
    """
    from ucode.config_io import read_json_safe

    sources = (
        settings,
        read_json_safe(project_dir / ".claude" / "settings.local.json"),
        read_json_safe(project_dir / ".claude" / "settings.json"),
        read_json_safe(user_settings_path),
    )
    for source in sources:
        status_line = source.get("statusLine")
        if not isinstance(status_line, dict):
            continue
        command = status_line.get("command")
        if isinstance(command, str) and command.strip():
            return status_line
    return None


def savings_status_line(
    original: dict | None,
    *,
    python: str,
    state_dir: Path,
    price_cache: Path,
    baseline_session_start: bool,
) -> dict:
    """The ``statusLine`` setting that prints ``original``'s row(s), then the savings row."""
    argv = [python, "-P", "-m", MODULE, "--state-dir", str(state_dir)]
    argv += ["--price-cache", str(price_cache)]
    if baseline_session_start:
        argv.append("--baseline-session-start")
    savings = shlex.join(argv)
    preserved = {
        key: original[key] for key in _PRESERVED_STATUS_LINE_KEYS if original and key in original
    }
    command = savings if original is None else _chain_commands(original["command"], savings)
    return {"type": "command", "command": command, **preserved}


def _chain_commands(original: str, savings: str) -> str:
    """Run ``original`` as Claude Code would have, then ``savings``, both on the same stdin.

    The original runs in a subshell of whichever shell Claude Code uses, so its shell-specific
    syntax keeps working and an ``exit`` in it can't skip the savings row. Command substitution
    drops its trailing newlines, so the savings row always starts on a line of its own.
    """
    return "\n".join(
        [
            "ug_statusline_input=$(cat)",
            "ug_statusline_base=$(printf '%s' \"$ug_statusline_input\" | (",
            original,
            ") )",
            '[ -n "$ug_statusline_base" ] && printf \'%s\\n\' "$ug_statusline_base"',
            f"printf '%s' \"$ug_statusline_input\" | {savings}",
        ]
    )


def prune_state(state_dir: Path, *, now: float | None = None) -> None:
    """Delete session state untouched for a week, including temp files from cancelled runs."""
    current = now if now is not None else time.time()
    try:
        entries = list(state_dir.iterdir())
    except OSError:
        return
    for path in entries:
        try:
            if path.is_file() and current - path.stat().st_mtime > STATE_RETENTION_SECONDS:
                path.unlink()
        except OSError:
            continue


@dataclass
class _FileTotals:
    """Running cost totals for one transcript file, resumable from ``offset``."""

    offset: int = 0
    actual: Decimal = Decimal(0)
    baseline: Decimal = Decimal(0)
    # Responses served by a model other than the baseline, i.e. where routing changed the model.
    rerouted: int = 0
    unpriced: list[str] = field(default_factory=list)
    # The last response's contribution, replaced (not re-added) when its id repeats.
    last_id: str | None = None
    last_actual: Decimal = Decimal(0)
    last_baseline: Decimal = Decimal(0)
    last_rerouted: int = 0

    def dump(self) -> dict[str, Any]:
        return {
            "offset": self.offset,
            "actual": str(self.actual),
            "baseline": str(self.baseline),
            "rerouted": self.rerouted,
            "unpriced": self.unpriced,
            "last_id": self.last_id,
            "last_actual": str(self.last_actual),
            "last_baseline": str(self.last_baseline),
            "last_rerouted": self.last_rerouted,
        }

    @classmethod
    def load(cls, raw: object) -> _FileTotals:
        if not isinstance(raw, dict):
            return cls()
        try:
            unpriced = raw.get("unpriced")
            last_id = raw.get("last_id")
            return cls(
                offset=int(raw.get("offset", 0)),
                actual=Decimal(str(raw.get("actual", 0))),
                baseline=Decimal(str(raw.get("baseline", 0))),
                rerouted=int(raw.get("rerouted", 0)),
                unpriced=[str(model) for model in unpriced] if isinstance(unpriced, list) else [],
                last_id=last_id if isinstance(last_id, str) else None,
                last_actual=Decimal(str(raw.get("last_actual", 0))),
                last_baseline=Decimal(str(raw.get("last_baseline", 0))),
                last_rerouted=int(raw.get("last_rerouted", 0)),
            )
        except (TypeError, ValueError, InvalidOperation):
            return cls()

    def add_response(
        self,
        record: object,
        prices: dict[str, ModelPrice],
        baseline_key: str,
        baseline_price: ModelPrice,
    ) -> None:
        if not isinstance(record, dict) or record.get("type") != "assistant":
            return
        message = record.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
        if not isinstance(message, dict) or not isinstance(usage, dict):
            return
        tokens = TokenUsage.from_message_usage(usage)
        if not any(tokens):
            return
        raw_model = message.get("model")
        model = raw_model if isinstance(raw_model, str) else ""
        served_key = model_key(model)
        served_price = prices.get(served_key)
        actual = token_cost(served_price, tokens) if served_price is not None else None
        baseline = token_cost(baseline_price, tokens)
        if actual is None or baseline is None:
            if model not in self.unpriced:
                self.unpriced.append(model)
            actual = baseline = Decimal(0)
        rerouted = int(served_key != baseline_key)

        # Claude Code writes one record per content block, each repeating the response's usage,
        # and one response's records are contiguous in its transcript.
        identity = message.get("id") or record.get("uuid")
        identity = identity if isinstance(identity, str) and identity else None
        if identity is not None and identity == self.last_id:
            self.actual -= self.last_actual
            self.baseline -= self.last_baseline
            self.rerouted -= self.last_rerouted
        self.actual += actual
        self.baseline += baseline
        self.rerouted += rerouted
        self.last_id = identity
        self.last_actual, self.last_baseline, self.last_rerouted = actual, baseline, rerouted

    def consume(
        self,
        path: Path,
        prices: dict[str, ModelPrice],
        baseline_key: str,
        baseline_price: ModelPrice,
    ) -> _FileTotals:
        """Fold in complete lines appended since ``offset``; returns the updated totals."""
        try:
            size = path.stat().st_size
        except OSError:
            return self
        totals = self if size >= self.offset else _FileTotals()  # rewritten: start over
        if size == totals.offset:
            return totals
        try:
            with path.open("rb") as handle:
                handle.seek(totals.offset)
                data = handle.read(size - totals.offset)
        except OSError:
            return totals
        end = data.rfind(b"\n")
        if end < 0:
            return totals  # only a partial line so far; Claude Code is still writing it
        totals.offset += end + 1
        for line in data[: end + 1].splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            totals.add_response(record, prices, baseline_key, baseline_price)
        return totals


def _model_ref(raw: object) -> dict[str, str] | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
        return None
    display_name = raw.get("display_name")
    return {
        "id": raw["id"],
        "display_name": display_name
        if isinstance(display_name, str) and display_name
        else raw["id"],
    }


def _state_path(state_dir: Path, session_id: str) -> Path:
    if _SAFE_SESSION_ID_RE.fullmatch(session_id):
        name = session_id
    else:
        name = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return state_dir / f"{name}.json"


def _read_state(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": _STATE_VERSION}
    if not isinstance(state, dict) or state.get("version") != _STATE_VERSION:
        return {"version": _STATE_VERSION}
    return state


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, separators=(",", ":"))
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _transcript_paths(transcript: Path) -> list[Path]:
    """The main transcript, then its subagents' (``<session>/subagents/agent-*.jsonl``)."""
    subagents = transcript.with_suffix("") / "subagents"
    return [transcript, *sorted(subagents.glob("agent-*.jsonl"))]


def format_savings(saved: Decimal, baseline: Decimal, baseline_label: str) -> str:
    """The row text; a negative ``saved`` (routing chose pricier models) is shown as such."""
    percent = Decimal(0)
    if baseline > 0:
        percent = (abs(saved) / baseline * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    magnitude = abs(saved)
    amount = (
        "<$0.01"
        if magnitude < _CENT
        else f"~${magnitude.quantize(_CENT, rounding=ROUND_HALF_UP):,}"
    )
    if saved >= 0:
        return f"Smart routing saved {amount} ({percent}%) vs {baseline_label}"
    return f"Smart routing cost {amount} more ({percent}%) than {baseline_label}"


def render(
    raw: str, *, state_dir: Path, price_cache: Path, baseline_session_start: bool
) -> str | None:
    """The savings row for one statusline payload, or None when there is nothing to claim.

    Hidden until some response ran on a model other than the baseline, and whenever any response
    can't be priced: an undercounted figure would be worse than none.
    """
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    session_id = payload.get("session_id")
    transcript = payload.get("transcript_path")
    current = _model_ref(payload.get("model"))
    if not isinstance(session_id, str) or not session_id:
        return None
    if not isinstance(transcript, str) or not transcript or current is None:
        return None

    state_path = _state_path(state_dir, session_id)
    state = _read_state(state_path)
    start = _model_ref(state.get("start_model"))
    # Recorded on the session's first refresh, before first-prompt routing can switch the model.
    if start is None:
        start = state["start_model"] = current
        _write_state(state_path, state)
    baseline = start if baseline_session_start else current

    cached = read_price_cache(price_cache)
    if cached is None:
        return None
    prices, fingerprint = cached
    baseline_key = model_key(baseline["id"])
    baseline_price = prices.get(baseline_key)
    if baseline_price is None:
        return None

    key = [baseline_key, fingerprint]
    files = state.get("files")
    if state.get("key") != key or not isinstance(files, dict):
        state["key"], files = key, {}
    actual_total = baseline_total = Decimal(0)
    rerouted = 0
    unpriced = False
    for path in _transcript_paths(Path(transcript)):
        totals = _FileTotals.load(files.get(str(path))).consume(
            path, prices, baseline_key, baseline_price
        )
        files[str(path)] = totals.dump()
        actual_total += totals.actual
        baseline_total += totals.baseline
        rerouted += totals.rerouted
        unpriced = unpriced or bool(totals.unpriced)
    state["files"] = files
    _write_state(state_path, state)

    if unpriced or rerouted <= 0 or baseline_total <= 0:
        return None
    return format_savings(baseline_total - actual_total, baseline_total, baseline["display_name"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=MODULE, description="Print the smart-routing savings row for a Claude Code session."
    )
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--price-cache", type=Path, required=True)
    parser.add_argument("--baseline-session-start", action="store_true")
    args = parser.parse_args(argv)
    try:
        line = render(
            sys.stdin.read(),
            state_dir=args.state_dir,
            price_cache=args.price_cache,
            baseline_session_start=args.baseline_session_start,
        )
        if line:
            sys.stdout.write(line + "\n")
    except Exception:  # noqa: BLE001 - a savings error must never break the user's status row
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
