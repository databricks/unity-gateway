"""Claude Code statusline for smart routing: a one-line row with the orchestrator plugin version,
whether routing is on, and an estimate of what routing saved this session.

``v2`` installs this as the session's ``statusLine`` command, wrapping any statusline the user
already had. The savings estimate assumes every token the session used, main agent and subagents
alike, would otherwise have run on the baseline main-agent model::

    saved = sum(tokens * price(baseline)) - sum(tokens * price(model that served them))

summed over every assistant response in the main and subagent transcripts, per token class. The
baseline is the main model the user chose; under first-prompt routing, where the router also picks
the main model, it is the model the session started on, before routing switched it, until the main
model changes again. The router switches it once, before the first answer, so a later change is the
user's and makes their choice the baseline again. A negative result (routing chose pricier models)
is shown as a cost increase.

The baseline is fixed per response when it is first read, never recomputed: a subagent response is
priced against the main model in effect at its timestamp (recorded as the main transcript is
read), and a main-agent response is its own baseline, so switching models with ``/model`` never
reprices earlier work.

Under Claude Code mods there is no statusline; ``v2`` hands the mod a pricer command instead
(``mod_pricer_argv``), and the mod runs it with ``--mod-usage`` on its own token sums.

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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

# Only ``ucode.constants`` may be imported here besides ``pricing``: anything heavier (``session_env``
# pulls in ``config_io``, hence tomlkit and Rich) would get a refresh cancelled.
from ucode.constants import SMART_ROUTING_ENV_KEYS, TRUTHY_ENV_VALUES
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
_STATE_VERSION = 2
# Env var naming the hooks' session-controls file; duplicated from ``session_env.SESSION_ENV_VAR``
# (a test keeps them equal) because importing that module is too heavy for a refresh.
SESSION_ENV_FILE_ENV_VAR = "UCODE_SESSION_ENV_FILE"
# The mods' pricer command (a JSON argv) and the token sums it prices (a file the mod writes next to
# the session env file); the mod reads both names, so change them together.
PRICER_ENV_VAR = "UCODE_SAVINGS_PRICER"
MOD_USAGE_FILENAME = "mod-usage.json"
_MOD_USAGE_VERSION = 1
# statusLine options that shape how the row renders rather than what it runs.
_PRESERVED_STATUS_LINE_KEYS = ("padding", "refreshInterval", "hideVimModeIndicator")
_SAFE_SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_CENT = Decimal("0.01")
# The Claude Code plugin whose skill drives smart routing's subagent delegation; the row shows its
# installed version. ug doesn't install it, so the row reads whatever Claude Code recorded on disk.
_ORCHESTRATOR_PLUGIN_NAME = "model-orchestrator"
_SYNTHETIC_MODEL = "<synthetic>"

# The baseline model key for a response, given the key of the model that served it, its timestamp
# (epoch seconds, None when the record has none), and the main agent's model timeline as read so
# far (``_FileTotals.models``).
BaselineFor = Callable[[str, float | None, list[list[Any]]], str]


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
    routing_enabled: bool,
    baseline_session_start: bool,
) -> dict:
    """The ``statusLine`` setting that prints ``original``'s row(s), then the smart-routing row."""
    argv = [*_module_argv(python), "--state-dir", str(state_dir)]
    argv += ["--price-cache", str(price_cache)]
    if routing_enabled:
        argv.append("--routing-enabled")
    if baseline_session_start:
        argv.append("--baseline-session-start")
    savings = shlex.join(argv)
    preserved = {
        key: original[key] for key in _PRESERVED_STATUS_LINE_KEYS if original and key in original
    }
    command = savings if original is None else _chain_commands(original["command"], savings)
    return {"type": "command", "command": command, **preserved}


def mod_pricer_argv(*, python: str, price_cache: Path, baseline_session_start: bool) -> list[str]:
    """The command the Claude Code mod runs (plus ``--mod-usage PATH``) to price its token sums."""
    argv = [*_module_argv(python), "--price-cache", str(price_cache)]
    if baseline_session_start:
        argv.append("--baseline-session-start")
    return argv


def _module_argv(python: str) -> list[str]:
    # -P keeps the launch directory off sys.path, so a project file can't shadow a stdlib module.
    return [python, "-P", "-m", MODULE]


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


def _epoch(raw: object) -> float | None:
    """Epoch seconds of a transcript record's ISO-8601 ``timestamp``, or None."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).timestamp()


def _response_costs(
    prices: dict[str, ModelPrice], served_key: str, baseline_key: str, tokens: TokenUsage
) -> tuple[Decimal, Decimal] | None:
    """``(actual, baseline)`` dollars for one response, or None when either model can't be priced."""
    served_price = prices.get(served_key)
    baseline_price = prices.get(baseline_key)
    if served_price is None or baseline_price is None:
        return None
    actual = token_cost(served_price, tokens)
    baseline = token_cost(baseline_price, tokens)
    if actual is None or baseline is None:
        return None
    return actual, baseline


def _claimable(
    actual: Decimal, baseline: Decimal, *, rerouted: bool
) -> tuple[Decimal, Decimal] | None:
    """``(saved, baseline)``, or None until routing changed some model and there is a cost to compare."""
    if not rerouted or baseline <= 0:
        return None
    return baseline - actual, baseline


def _own_baseline(served_key: str, when: float | None, models: list[list[Any]]) -> str:
    """A response is its own baseline, so it is never counted as rerouted."""
    return served_key


def _timeline_baseline(fallback: str) -> BaselineFor:
    """A subagent response's baseline is the main model in effect when it ran.

    The timeline is the main agent's ``[epoch, model_key]`` changes, in transcript order. A response
    before the first recorded change gets the earliest model, one without a timestamp the latest,
    and ``fallback`` applies when the main transcript shows no model at all.
    """

    def baseline_for(served_key: str, when: float | None, models: list[list[Any]]) -> str:
        if not models:
            return fallback
        if when is None:
            return models[-1][1]
        for epoch, key in reversed(models):
            if epoch <= when:
                return key
        return models[0][1]

    return baseline_for


def _first_prompt_baseline(start_key: str, after: BaselineFor) -> BaselineFor:
    """The session's start model while the main agent is still on its first model, then ``after``.

    The router switches the main model once, before its first answer, so any later change is the
    user's: pricing past it against the start model would credit routing with the user's choice.
    """

    def baseline_for(served_key: str, when: float | None, models: list[list[Any]]) -> str:
        if len(models) < 2 or (when is not None and when < models[1][0]):
            return start_key
        return after(served_key, when, models)

    return baseline_for


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
    # Main transcript only: ``[epoch, model_key]`` each time the main agent's model changed, so a
    # later ``/model`` switch doesn't reprice the work done before it.
    models: list[list[Any]] = field(default_factory=list)

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
            "models": self.models,
        }

    @classmethod
    def load(cls, raw: object) -> _FileTotals:
        if not isinstance(raw, dict):
            return cls()
        try:
            unpriced = raw.get("unpriced")
            last_id = raw.get("last_id")
            models = raw.get("models")
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
                models=[[float(epoch), str(key)] for epoch, key in models]
                if isinstance(models, list)
                else [],
            )
        except (TypeError, ValueError, InvalidOperation):
            return cls()

    def _note_main_model(self, key: str, when: float | None) -> None:
        if self.models and self.models[-1][1] == key:
            return
        # A record with no timestamp sorts with the one before it.
        epoch = when if when is not None else (self.models[-1][0] if self.models else 0.0)
        self.models.append([epoch, key])

    def add_response(
        self,
        record: object,
        prices: dict[str, ModelPrice],
        baseline_for: BaselineFor,
        *,
        timeline: list[list[Any]] | None = None,
    ) -> None:
        """Fold in one transcript record.

        ``timeline`` is the main agent's, for a subagent transcript; without one this is the main
        transcript, which records its own in ``models`` as it is read.
        """
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
        when = _epoch(record.get("timestamp"))
        if timeline is None and model and model != _SYNTHETIC_MODEL:
            self._note_main_model(served_key, when)
        baseline_key = baseline_for(served_key, when, self.models if timeline is None else timeline)
        costs = _response_costs(prices, served_key, baseline_key, tokens)
        if costs is None:
            if model not in self.unpriced:
                self.unpriced.append(model)
            actual = baseline = Decimal(0)
        else:
            actual, baseline = costs
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
        baseline_for: BaselineFor,
        *,
        timeline: list[list[Any]] | None = None,
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
            totals.add_response(record, prices, baseline_for, timeline=timeline)
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
    if not isinstance(state, dict):
        return {"version": _STATE_VERSION}
    if state.get("version") != _STATE_VERSION:
        # Only start_model survives a format change: it can't be re-captured once routing has
        # switched the session's model, and the rest is rebuilt from the transcripts.
        start = _model_ref(state.get("start_model"))
        return {"version": _STATE_VERSION, **({} if start is None else {"start_model": start})}
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


def _claude_config_dir() -> Path:
    """Claude Code's config directory, honoring ``CLAUDE_CONFIG_DIR`` as Claude Code does."""
    configured = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return Path(configured) if configured else Path.home() / ".claude"


def orchestrator_plugin_version(config_dir: Path | None = None) -> str | None:
    """The installed ``model-orchestrator`` plugin version, or None when it can't be determined.

    ug doesn't install the plugin, so this reads whatever Claude Code recorded in
    ``plugins/installed_plugins.json``; a missing file, unexpected shape, or absent plugin yields
    None and the row simply omits the version rather than inventing one.
    """
    base = config_dir if config_dir is not None else _claude_config_dir()
    try:
        payload = json.loads((base / "plugins" / "installed_plugins.json").read_text("utf-8"))
    except (OSError, ValueError):
        return None
    plugins = payload.get("plugins") if isinstance(payload, dict) else None
    if not isinstance(plugins, dict):
        return None
    for key, records in plugins.items():
        # Keys are "<name>@<marketplace>"; match on the name so the marketplace can vary.
        if not isinstance(key, str) or key.split("@", 1)[0] != _ORCHESTRATOR_PLUGIN_NAME:
            continue
        for record in records if isinstance(records, list) else []:
            version = record.get("version") if isinstance(record, dict) else None
            if isinstance(version, str) and version and version != "unknown":
                return version
    return None


def _savings_figures(saved: Decimal, baseline: Decimal) -> tuple[str, Decimal]:
    """``saved``'s magnitude as dollars (to the cent) and as a whole percent of ``baseline``."""
    percent = Decimal(0)
    if baseline > 0:
        percent = (abs(saved) / baseline * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    magnitude = abs(saved)
    amount = (
        "<$0.01" if magnitude < _CENT else f"${magnitude.quantize(_CENT, rounding=ROUND_HALF_UP):,}"
    )
    return amount, percent


def _savings_text(saved: Decimal, baseline: Decimal) -> str:
    """The savings clause: a money-bag estimate when positive, an honest cost increase when not."""
    amount, percent = _savings_figures(saved, baseline)
    if saved >= 0:
        return f"💰 Est. saved with smart routing: {amount} ({percent}%)"
    return f"Smart routing cost {amount} more ({percent}%)"


def _mod_savings_text(saved: Decimal, baseline: Decimal) -> str:
    """The mod's savings segment; the mod's band already says it is about smart routing."""
    amount, percent = _savings_figures(saved, baseline)
    if saved >= 0:
        return f"💰 Est. saved {amount} ({percent}%)"
    return f"cost {amount} more ({percent}%)"


def _compute_savings(
    raw: str, *, state_dir: Path, price_cache: Path, baseline_session_start: bool
) -> tuple[Decimal, Decimal] | None:
    """The ``(saved, baseline)`` dollars for one statusline payload, or None with nothing to claim.

    None until some response ran on a model other than its baseline, and whenever any response
    can't be priced: an undercounted figure would be worse than none. The caller then shows "on".
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

    cached = read_price_cache(price_cache)
    if cached is None:
        return None
    prices, fingerprint = cached
    # First-prompt routing prices work against the model the session started on. Otherwise the
    # baseline is whatever main model each response ran beside, so it isn't part of the key.
    start_key = model_key(start["id"]) if baseline_session_start else None

    key = [start_key, fingerprint]
    files = state.get("files")
    if state.get("key") != key or not isinstance(files, dict):
        state["key"], files = key, {}
    main_path, *subagent_paths = _transcript_paths(Path(transcript))
    actual_total = baseline_total = Decimal(0)
    rerouted = 0
    unpriced = False

    def fold(
        path: Path, baseline_for: BaselineFor, *, timeline: list[list[Any]] | None
    ) -> _FileTotals:
        nonlocal actual_total, baseline_total, rerouted, unpriced
        totals = _FileTotals.load(files.get(str(path))).consume(
            path, prices, baseline_for, timeline=timeline
        )
        files[str(path)] = totals.dump()
        actual_total += totals.actual
        baseline_total += totals.baseline
        rerouted += totals.rerouted
        unpriced = unpriced or bool(totals.unpriced)
        return totals

    main_baseline: BaselineFor = _own_baseline
    subagent_baseline = _timeline_baseline(model_key(current["id"]))
    if start_key is not None:
        main_baseline = _first_prompt_baseline(start_key, main_baseline)
        subagent_baseline = _first_prompt_baseline(start_key, subagent_baseline)
    # The main transcript is read first so its model timeline covers the subagent responses.
    main_totals = fold(main_path, main_baseline, timeline=None)
    for path in subagent_paths:
        fold(path, subagent_baseline, timeline=main_totals.models)
    state["files"] = files
    _write_state(state_path, state)

    if unpriced:
        return None
    return _claimable(actual_total, baseline_total, rerouted=rerouted > 0)


def _mod_usage_savings(
    usage_path: Path, *, price_cache: Path, baseline_session_start: bool
) -> tuple[Decimal, Decimal] | None:
    """The ``(saved, baseline)`` dollars for the mod's token sums, or None with nothing to claim.

    ``usage_path`` holds the mod's tokens pre-aggregated per (agent, baseline, served); a malformed
    or baseline-less entry, like an unpriced one, voids the estimate rather than undercount it.
    Under first-prompt routing, entries from before the user changed the main model use the
    session's start model as their baseline. A file that can't be read as the mod's format at all is
    an error, not an empty estimate.
    """
    document = json.loads(usage_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("version") != _MOD_USAGE_VERSION:
        raise ValueError(f"unsupported mod usage file: {usage_path}")
    entries = document.get("entries")
    if not isinstance(entries, list):
        raise ValueError(f"unsupported mod usage file: {usage_path}")
    fixed_baseline = None
    if baseline_session_start:
        start = document.get("start_model")
        if not isinstance(start, str) or not start:
            return None
        fixed_baseline = start
    cached = read_price_cache(price_cache)
    if cached is None:
        return None
    prices = cached[0]

    actual_total = baseline_total = Decimal(0)
    rerouted = False
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        usage, served = entry.get("usage"), entry.get("served")
        baseline = entry.get("baseline")
        if fixed_baseline is not None and entry.get("before_user_switch") is True:
            baseline = fixed_baseline
        if not isinstance(usage, dict) or not isinstance(served, str) or not served:
            return None
        if not isinstance(baseline, str) or not baseline:
            return None
        # The mod's usage has no cache-write TTL split, and ug launches Claude with 1-hour caching.
        tokens = TokenUsage.from_message_usage(usage, uncovered_writes_1h=True)
        if not any(tokens):
            continue
        served_key, baseline_key = model_key(served), model_key(baseline)
        costs = _response_costs(prices, served_key, baseline_key, tokens)
        if costs is None:
            return None
        actual_total += costs[0]
        baseline_total += costs[1]
        rerouted = rerouted or served_key != baseline_key
    return _claimable(actual_total, baseline_total, rerouted=rerouted)


def mod_usage_output(usage_path: Path, *, price_cache: Path, baseline_session_start: bool) -> str:
    """The one JSON line the mod reads: its savings segment and plugin segment, each or null."""
    # Each segment fails alone, and the mod must get a parseable line whatever goes wrong.
    savings = plugin = None
    try:
        computed = _mod_usage_savings(
            usage_path, price_cache=price_cache, baseline_session_start=baseline_session_start
        )
        savings = None if computed is None else _mod_savings_text(*computed)
    except Exception:  # noqa: BLE001
        pass
    try:
        version = orchestrator_plugin_version()
        plugin = f"plugin v{version}" if version else None
    except Exception:  # noqa: BLE001
        pass
    return json.dumps({"savings": savings, "plugin": plugin}, ensure_ascii=False)


def _routing_on(launch_enabled: bool) -> bool:
    """Whether smart routing is on now, following the session's ``/smart-router`` toggle.

    The launch decides whether a session is routed at all: a plain launch never routes, and could
    inherit an outer routed session's file. For a routed one, the session file (which the toggle
    rewrites but the statusLine command can't be) overrides the launch-time flag when it names a
    routing flag; an empty file, as after turning routing back on, leaves the launch's choice.
    """
    if not launch_enabled:
        return False
    path = os.environ.get(SESSION_ENV_FILE_ENV_VAR, "").strip()
    if not path:
        return True
    try:
        values = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    if not isinstance(values, dict):
        return True
    flags = [values[name] for name in SMART_ROUTING_ENV_KEYS if name in values]
    if not flags:
        return True
    return any(
        isinstance(flag, str) and flag.strip().lower() in TRUTHY_ENV_VALUES for flag in flags
    )


def render(
    raw: str,
    *,
    routing_enabled: bool,
    state_dir: Path,
    price_cache: Path,
    baseline_session_start: bool,
) -> str:
    """The one-line smart-routing status row.

    ``off`` when routing is disabled (at launch, or since by ``/smart-router``), ``on`` once it is
    on but no estimate exists yet, otherwise the estimate. The orchestrator plugin version is
    appended when it can be read.
    """
    version = orchestrator_plugin_version()
    plugin = f" · smart router plugin v{version}" if version else ""
    if not _routing_on(routing_enabled):
        return f"Smart routing off{plugin}"
    computed = _compute_savings(
        raw,
        state_dir=state_dir,
        price_cache=price_cache,
        baseline_session_start=baseline_session_start,
    )
    if computed is None:
        return f"Smart routing on{plugin}"
    return f"{_savings_text(*computed)}{plugin}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=MODULE,
        description="Print the smart-routing status row for a Claude Code session, or (with "
        "--mod-usage) price the Claude Code mod's token sums as a JSON line.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--state-dir", type=Path)
    source.add_argument("--mod-usage", type=Path)
    parser.add_argument("--price-cache", type=Path, required=True)
    parser.add_argument("--routing-enabled", action="store_true")
    parser.add_argument("--baseline-session-start", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.mod_usage is not None:
            line = mod_usage_output(
                args.mod_usage,
                price_cache=args.price_cache,
                baseline_session_start=args.baseline_session_start,
            )
        else:
            line = render(
                sys.stdin.read(),
                routing_enabled=args.routing_enabled,
                state_dir=args.state_dir,
                price_cache=args.price_cache,
                baseline_session_start=args.baseline_session_start,
            )
        # Write bytes so the middle-dot survives a non-UTF-8 stdout locale.
        sys.stdout.buffer.write((line + "\n").encode("utf-8"))
    except Exception:  # noqa: BLE001 - a status error must never break the user's status row
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
