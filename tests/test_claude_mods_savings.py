"""Tests for the savings concern of ug's smart-routing Claude Code UI mod."""

from __future__ import annotations

import json
from pathlib import Path

from ucode import mods
from ucode.smart_routing.claude_statusline import (
    MOD_USAGE_FILENAME,
    PRICER_ENV_VAR,
    SESSION_ENV_FILE_ENV_VAR,
)

MODS_DIR = Path(__file__).resolve().parents[1] / "typescript" / "claude-mods"
SAVINGS_TS = "smart-routing-savings.ts"


def _source(name: str) -> str:
    return (MODS_DIR / name).read_text(encoding="utf-8")


def test_smart_routing_ui_ships_the_savings_concern():
    assert SAVINGS_TS in mods.SMART_ROUTING_UI.extra


def test_write_mod_copies_the_savings_concern(tmp_path):
    plugin_dir = tmp_path / "routing-plugin"

    mods.write_mod(plugin_dir, mods.SMART_ROUTING_UI)

    hooks_dir = plugin_dir / "hooks"
    assert json.loads((hooks_dir / "hooks.json").read_text()) == {"modules": ["./register.ts"]}
    assert (hooks_dir / SAVINGS_TS).read_text() == _source(SAVINGS_TS)


def test_entry_composes_savings_and_the_band_draws_its_segments():
    entry = _source("register.ts")
    assert "import { registerSavings } from './smart-routing-savings'" in entry
    assert "registerSavings(on)" in entry

    status = _source("smart-routing-status.ts")
    assert "import { savingsSegments } from './smart-routing-savings'" in status
    assert "savingsSegments()" in status


def test_savings_concern_matches_the_python_contract():
    source = _source(SAVINGS_TS)
    # $.env.get needs string literals, so the names appear verbatim.
    assert f"'{PRICER_ENV_VAR}'" in source
    assert f"'{MOD_USAGE_FILENAME}'" in source
    assert "'--mod-usage'" in source
    assert f"'{SESSION_ENV_FILE_ENV_VAR}'" in source
    # One pricer line out: {"savings": ..., "plugin": ...}.
    assert "out.savings" in source and "out.plugin" in source
    # Document in: {"version": 1, "start_model": ..., "main_model": ..., "first_main_model": ...,
    # "user_switched": ..., "entries": [{..., "before_user_switch": ...}]}; Python ignores the
    # main-model fields, the mod reads them back to seed itself after a reload.
    for field in (
        "version: 1,",
        "start_model: startModel,",
        "main_model: lastMainModel,",
        "first_main_model: firstMainModel,",
        "user_switched: userSwitched,",
        "entries: Array.from(entries.values())",
        "before_user_switch: beforeUserSwitch,",
    ):
        assert field in source, field
    assert "saved.version !== 1" in source
    for field in ("main_model", "start_model", "first_main_model", "user_switched"):
        assert f"saved.{field}" in source, field
    assert "item.before_user_switch === true" in source


def test_savings_concern_keeps_the_baseline_and_start_model_rules():
    source = _source(SAVINGS_TS)
    # Subagents price against the main model's request id; main is its own baseline.
    assert "lastMainModel = str(request.model) ?? lastMainModel" in source
    assert "const base = agent === null ? served : baseline" in source
    # Served is priced by the model service the request named, not the drifting id reported back.
    assert "const served = str(request.model) ?? str(usage?.model)" in source
    # The start model is only ever set once, so a later session.start cannot overwrite it.
    assert "startModel ??=" in source
    assert "startModel = " not in source.replace("startModel ??=", "")
    # First-prompt routing switches the main model before its first request, so a main request
    # on another model marks a user switch; each request is flagged before it runs.
    assert "firstMainModel ??= lastMainModel" in source
    assert "userSwitched ||= lastMainModel !== firstMainModel" in source
    assert source.index("const beforeUserSwitch = !userSwitched") < source.index(
        "const result = yield* next(e)"
    )
    # Every turn.step counts (side queries are not turn steps): no turn-id filter.
    assert "mainTurns" not in source


def test_savings_concern_hooks_the_events_it_needs():
    source = _source(SAVINGS_TS)
    for event in ("session.start", "turn.step", "turn.complete"):
        assert f"on('{event}'" in source
    assert "on('turn.start'" not in source


def test_savings_concern_recovers_from_failures():
    source = _source(SAVINGS_TS)
    # A failed pricer run drops the stale estimate; a failed write is retried next turn.
    assert "savings: null, plugin: priced.plugin" in source
    assert "dirty = true\n    throw error" in source


def test_only_the_savings_concern_hooks_the_turn_events():
    # Registering an event twice without a matcher fails the whole hooks module,
    # so the concerns composed by register.ts must not share these events.
    for name in ("smart-routing-status.ts", "subagent-routing.ts", "register.ts"):
        source = _source(name)
        for event in ("session.start", "turn.start", "turn.step", "turn.complete"):
            assert f"on('{event}'" not in source, (name, event)
