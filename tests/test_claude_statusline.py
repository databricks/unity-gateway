"""Tests for the Claude Code smart-routing statusline row."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ucode.constants import ENABLE_SMART_ROUTING_ENV_VAR, ENABLE_SUBAGENT_ROUTING_ENV_VAR
from ucode.smart_routing import claude_statusline, pricing, session_env
from ucode.smart_routing.pricing import ModelPrice

OPUS_ID = "system.ai.claude-opus-4-8"
SONNET_ID = "system.ai.claude-sonnet-5"
OPUS = {"id": OPUS_ID, "display_name": "Opus 4.8"}
SONNET = {"id": SONNET_ID, "display_name": "Sonnet 5"}
PRICES = {
    OPUS_ID: ModelPrice(
        input=Decimal("5"),
        output=Decimal("25"),
        cache_read=Decimal("0.5"),
        cache_write_5m=Decimal("6.25"),
        cache_write_1h=Decimal("10"),
    ),
    SONNET_ID: ModelPrice(
        input=Decimal("2"),
        output=Decimal("10"),
        cache_read=Decimal("0.2"),
        cache_write_5m=Decimal("2.5"),
        cache_write_1h=Decimal("4"),
    ),
}
# Opus main-agent response: 10*5 + 100k*0.5 + 1k*25 = $0.07505 (same either way).
MAIN_USAGE = {"input_tokens": 10, "cache_read_input_tokens": 100_000, "output_tokens": 1_000}
# Subagent response: $0.122 on Sonnet (1k*2 + 20k*4 + 4k*10), $0.305 at Opus rates.
SUBAGENT_USAGE = {
    "input_tokens": 1_000,
    "cache_creation_input_tokens": 20_000,
    "cache_creation": {"ephemeral_1h_input_tokens": 20_000, "ephemeral_5m_input_tokens": 0},
    "output_tokens": 4_000,
}


# Transcript timestamps, oldest first.
T0, T1, T2, T3 = (f"2026-05-01T10:0{minute}:00.000Z" for minute in range(4))
HAIKU_ID = "system.ai.claude-haiku-4-5"


def epoch(timestamp: str) -> float:
    return datetime.fromisoformat(timestamp).timestamp()


def response(
    message_id: str, model: str, usage: dict, block: str = "text", at: str | None = None
) -> dict:
    record = {
        "type": "assistant",
        "uuid": f"{message_id}-{block}",
        "message": {"id": message_id, "model": model, "usage": usage, "content": [{"type": block}]},
    }
    if at is not None:
        record["timestamp"] = at
    return record


def append(path: Path, *records: dict, trailing_newline: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(record) for record in records)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text + ("\n" if trailing_newline else ""))


def write_plugin_version(
    config_dir: Path,
    version: str,
    *,
    name: str = "model-orchestrator",
    marketplace: str = "example-marketplace",
) -> None:
    """Write a Claude Code ``installed_plugins.json`` recording ``name`` at ``version``."""
    plugins_dir = config_dir / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    (plugins_dir / "installed_plugins.json").write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {f"{name}@{marketplace}": [{"scope": "user", "version": version}]},
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def no_session_env_file(monkeypatch) -> None:
    """Render reads the hooks' session file named by this env var; keep the developer's out."""
    monkeypatch.delenv(claude_statusline.SESSION_ENV_FILE_ENV_VAR, raising=False)


@pytest.fixture(autouse=True)
def claude_config_dir(tmp_path, monkeypatch) -> Path:
    """Point the row's plugin-version lookup at an empty per-test config dir (no version by default).

    The row reads Claude Code's plugin state via ``CLAUDE_CONFIG_DIR``; isolating it keeps the
    developer's real installed plugins out of the assertions.
    """
    config_dir = tmp_path / "claude-config"
    config_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    return config_dir


class Session:
    """One Claude Code session's transcript layout plus ug's statusline state."""

    def __init__(self, root: Path, session_id: str = "session-1") -> None:
        self.session_id = session_id
        self.transcript = root / "project" / "transcript.jsonl"
        self.transcript.parent.mkdir(parents=True)
        self.transcript.touch()
        self.state_dir = root / "claude-savings"
        self.price_cache = root / "model-prices.json"

    def subagent(self, agent_id: str) -> Path:
        return self.transcript.with_suffix("") / "subagents" / f"agent-{agent_id}.jsonl"

    def payload(self, model: dict) -> str:
        return json.dumps(
            {"session_id": self.session_id, "transcript_path": str(self.transcript), "model": model}
        )

    def render(
        self,
        model: dict = OPUS,
        *,
        baseline_session_start: bool = False,
        routing_enabled: bool = True,
    ) -> str:
        return claude_statusline.render(
            self.payload(model),
            routing_enabled=routing_enabled,
            state_dir=self.state_dir,
            price_cache=self.price_cache,
            baseline_session_start=baseline_session_start,
        )


@pytest.fixture
def session(tmp_path) -> Session:
    session = Session(tmp_path)
    pricing.write_price_cache(session.price_cache, PRICES, now=1.0)
    return session


class TestRender:
    def test_prices_all_tokens_at_main_model_minus_actual_cost(self, session):
        append(
            session.transcript,
            response("msg-main", OPUS_ID, MAIN_USAGE, "text"),
            response("msg-main", OPUS_ID, MAIN_USAGE, "tool_use"),
        )
        append(session.subagent("a1"), response("msg-sub", SONNET_ID, SUBAGENT_USAGE))

        # Baseline $0.38005 (all tokens at Opus) - actual $0.19705 = $0.183 saved (48%).
        assert session.render() == "💰 Est. saved with smart routing: $0.18 (48%)"

    def test_prices_a_served_bedrock_id_with_its_system_ai_rate(self, session):
        haiku = ModelPrice(
            input=Decimal("1"),
            output=Decimal("5"),
            cache_read=Decimal("0.1"),
            cache_write_5m=Decimal("1.25"),
            cache_write_1h=Decimal("2"),
        )
        # Endpoint rates are keyed by system.ai name; Haiku responses carry its Bedrock id.
        pricing.write_price_cache(
            session.price_cache, {**PRICES, "system.ai.claude-haiku-4-5": haiku}, now=2.0
        )
        append(
            session.subagent("a1"),
            response("msg-sub", "anthropic.claude-haiku-4-5-20251001-v1:0", SUBAGENT_USAGE),
        )

        # $0.061 on Haiku (1k*1 + 20k*2 + 4k*5) vs $0.305 at Opus rates.
        assert session.render() == "💰 Est. saved with smart routing: $0.24 (80%)"

    def test_counts_a_response_split_across_records_once(self, session):
        append(session.subagent("a1"), response("msg-sub", SONNET_ID, SUBAGENT_USAGE))
        once = session.render()
        append(session.subagent("a2"), response("msg-other", SONNET_ID, SUBAGENT_USAGE, "text"))
        append(session.subagent("a2"), response("msg-other", SONNET_ID, SUBAGENT_USAGE, "tool_use"))

        assert once == "💰 Est. saved with smart routing: $0.18 (60%)"
        assert session.render() == "💰 Est. saved with smart routing: $0.37 (60%)"

    def test_reads_transcripts_incrementally(self, session):
        append(session.subagent("a1"), response("msg-sub", SONNET_ID, SUBAGENT_USAGE))
        first = session.render()
        append(
            session.subagent("a1"),
            response("msg-late", SONNET_ID, SUBAGENT_USAGE),
            trailing_newline=False,
        )

        assert first == "💰 Est. saved with smart routing: $0.18 (60%)"
        # A line Claude Code is still writing is left for the next refresh.
        assert session.render() == first
        with session.subagent("a1").open("a", encoding="utf-8") as handle:
            handle.write("\n")
        assert session.render() == "💰 Est. saved with smart routing: $0.37 (60%)"

    def test_starts_over_when_a_transcript_is_rewritten(self, session):
        append(
            session.subagent("a1"),
            *(response(f"m{i}", SONNET_ID, SUBAGENT_USAGE) for i in range(3)),
        )
        assert session.render() == "💰 Est. saved with smart routing: $0.55 (60%)"
        session.subagent("a1").write_text(
            json.dumps(response("m0", SONNET_ID, SUBAGENT_USAGE)) + "\n"
        )

        assert session.render() == "💰 Est. saved with smart routing: $0.18 (60%)"

    def test_shows_a_net_cost_increase_honestly(self, session):
        append(session.transcript, response("msg-main", SONNET_ID, MAIN_USAGE))
        append(session.subagent("a1"), response("msg-sub", OPUS_ID, SUBAGENT_USAGE))

        # Baseline $0.152 (all at Sonnet) vs actual $0.335: routing cost $0.183 more.
        assert session.render(SONNET) == "Smart routing cost $0.18 more (120%)"

    def test_baseline_follows_the_main_model_the_user_chose(self, tmp_path, session):
        other = Session(tmp_path / "other")
        pricing.write_price_cache(other.price_cache, PRICES, now=1.0)
        for each in (session, other):
            append(each.subagent("a1"), response("msg-sub", SONNET_ID, SUBAGENT_USAGE))

        assert session.render(OPUS) == "💰 Est. saved with smart routing: $0.18 (60%)"
        # Nothing ran on a model other than a Sonnet baseline, so there is nothing to claim yet.
        assert other.render(SONNET) == "Smart routing on"

    def test_first_prompt_routing_uses_the_pre_routing_model(self, session):
        # The first refresh happens before the first prompt is routed: nothing to claim yet.
        assert session.render(OPUS, baseline_session_start=True) == "Smart routing on"
        append(session.transcript, response("msg-main", SONNET_ID, MAIN_USAGE))

        # Main-agent tokens the router moved to Sonnet count toward savings too:
        # $0.07505 at Opus vs 10*2 + 100k*0.2 + 1k*10 = $0.03002 at Sonnet.
        assert (
            session.render(SONNET, baseline_session_start=True)
            == "💰 Est. saved with smart routing: $0.05 (60%)"
        )

    def test_reprices_when_the_price_cache_refreshes(self, session):
        append(session.subagent("a1"), response("msg-sub", SONNET_ID, SUBAGENT_USAGE))
        before = session.render()
        pricing.write_price_cache(
            session.price_cache,
            {
                **PRICES,
                SONNET_ID: ModelPrice(
                    input=Decimal("5"),
                    output=Decimal("25"),
                    cache_read=Decimal("0.5"),
                    cache_write_5m=Decimal("6.25"),
                    cache_write_1h=Decimal("10"),
                ),
            },
            now=2.0,
        )

        assert before == "💰 Est. saved with smart routing: $0.18 (60%)"
        assert session.render() == "💰 Est. saved with smart routing: <$0.01 (0%)"

    @pytest.mark.parametrize("model", ["system.ai.claude-mystery-1", ""])
    def test_shows_on_when_a_response_cannot_be_priced(self, session, model):
        append(session.subagent("a1"), response("msg-sub", SONNET_ID, SUBAGENT_USAGE))
        append(session.subagent("a2"), response("msg-x", model, SUBAGENT_USAGE))

        # An unpriceable response would undercount the estimate, so the row falls back to "on".
        assert session.render() == "Smart routing on"

    def test_shows_on_without_prices(self, session):
        session.price_cache.unlink()
        append(session.subagent("a1"), response("msg-sub", SONNET_ID, SUBAGENT_USAGE))

        assert session.render() == "Smart routing on"

    def test_shows_on_when_the_baseline_model_is_unpriced(self, session):
        append(session.subagent("a1"), response("msg-sub", SONNET_ID, SUBAGENT_USAGE))

        model = {"id": "system.ai.claude-unknown", "display_name": "?"}
        assert session.render(model) == "Smart routing on"

    def test_shows_the_orchestrator_plugin_version(self, session, claude_config_dir):
        write_plugin_version(claude_config_dir, "0.4.4")
        append(session.subagent("a1"), response("msg-sub", SONNET_ID, SUBAGENT_USAGE))

        assert session.render() == (
            "💰 Est. saved with smart routing: $0.18 (60%) · smart router plugin v0.4.4"
        )

    def test_routing_disabled_shows_off(self, session):
        # routing_enabled=False short-circuits before reading any transcript; empty is fine.
        assert session.render(routing_enabled=False) == "Smart routing off"

    def test_routing_disabled_with_plugin_version(self, session, claude_config_dir):
        write_plugin_version(claude_config_dir, "0.4.4")
        assert (
            session.render(routing_enabled=False)
            == "Smart routing off · smart router plugin v0.4.4"
        )

    def test_ignores_responses_without_usage(self, session):
        append(
            session.transcript,
            response("msg-synthetic", "<synthetic>", {"input_tokens": 0, "output_tokens": 0}),
            {"type": "user", "message": {"content": "hi"}},
            {"type": "assistant", "message": "not a dict"},
        )
        append(session.subagent("a1"), response("msg-sub", SONNET_ID, SUBAGENT_USAGE))

        assert session.render() == "💰 Est. saved with smart routing: $0.18 (60%)"

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "not json",
            "[]",
            json.dumps({"session_id": "s", "model": OPUS}),
            json.dumps({"session_id": "s", "transcript_path": "/x.jsonl", "model": {}}),
        ],
    )
    def test_malformed_payload_still_shows_the_row(self, session, raw):
        # No usable payload means no savings, but the version/state row still renders.
        assert (
            claude_statusline.render(
                raw,
                routing_enabled=True,
                state_dir=session.state_dir,
                price_cache=session.price_cache,
                baseline_session_start=False,
            )
            == "Smart routing on"
        )

    def test_unsafe_session_ids_do_not_escape_the_state_dir(self, tmp_path):
        session = Session(tmp_path, session_id="../../escape")
        pricing.write_price_cache(session.price_cache, PRICES)

        session.render()

        hashed = hashlib.sha256(b"../../escape").hexdigest()
        assert [path.name for path in session.state_dir.iterdir()] == [f"{hashed}.json"]


class TestMainModelTimeline:
    """The baseline is fixed per response, so `/model` never reprices work already done."""

    def test_switching_the_main_model_does_not_reprice_earlier_work(self, session):
        append(session.transcript, response("m1", OPUS_ID, MAIN_USAGE, at=T0))
        append(session.subagent("a1"), response("s1", SONNET_ID, SUBAGENT_USAGE, at=T1))
        # Baseline $0.38005 (main + subagent at Opus) - actual $0.19705.
        assert session.render(OPUS) == "💰 Est. saved with smart routing: $0.18 (48%)"

        # `/model sonnet`: the first subagent still counts against the Opus it ran beside, while
        # work after the switch is measured against Sonnet (the old pricing would show "on").
        append(session.transcript, response("m2", SONNET_ID, MAIN_USAGE, at=T2))
        append(session.subagent("a1"), response("s2", SONNET_ID, SUBAGENT_USAGE, at=T3))

        # Baseline $0.53207 (adds $0.03002 + $0.122 at Sonnet) - actual $0.34907.
        assert session.render(SONNET) == "💰 Est. saved with smart routing: $0.18 (34%)"

    def test_a_subagent_response_uses_the_main_model_in_effect_at_its_timestamp(self, session):
        append(
            session.transcript,
            response("m1", OPUS_ID, MAIN_USAGE, at=T0),
            response("m2", SONNET_ID, MAIN_USAGE, at=T2),
        )
        append(
            session.subagent("a1"),
            response("s1", SONNET_ID, SUBAGENT_USAGE, at=T1),
            response("s2", SONNET_ID, SUBAGENT_USAGE, at=T3),
        )

        # Only s1 ran beside Opus; s2 ran beside Sonnet, the main model by then.
        assert session.render(SONNET) == "💰 Est. saved with smart routing: $0.18 (34%)"

    def test_a_subagent_response_without_a_timestamp_uses_the_latest_main_model(self, session):
        append(
            session.transcript,
            response("m1", OPUS_ID, MAIN_USAGE, at=T0),
            response("m2", SONNET_ID, MAIN_USAGE, at=T2),
        )
        append(session.subagent("a1"), response("s1", SONNET_ID, SUBAGENT_USAGE))

        assert session.render(OPUS) == "Smart routing on"

    def test_a_subagent_response_before_the_first_main_record_uses_the_earliest_model(
        self, session
    ):
        append(session.transcript, response("m1", OPUS_ID, MAIN_USAGE, at=T1))
        append(session.subagent("a1"), response("s1", SONNET_ID, SUBAGENT_USAGE, at=T0))

        assert session.render(SONNET) == "💰 Est. saved with smart routing: $0.18 (48%)"

    def test_main_agent_responses_are_their_own_baseline(self, session):
        # Choosing a cheaper main model with `/model` is the user's call, not routing's saving.
        append(session.transcript, response("m1", SONNET_ID, MAIN_USAGE, at=T0))

        assert session.render(OPUS) == "Smart routing on"

    def test_timeline_records_main_model_changes_only_and_persists(self, session):
        append(
            session.transcript,
            response("m1", OPUS_ID, MAIN_USAGE, "text", at=T0),
            response("m1", OPUS_ID, MAIN_USAGE, "tool_use", at=T0),
            response("m2", OPUS_ID, MAIN_USAGE, at=T1),
        )
        append(session.subagent("a1"), response("s1", HAIKU_ID, SUBAGENT_USAGE, at=T1))
        session.render()
        append(
            session.transcript,
            response("m-synthetic", "<synthetic>", {"input_tokens": 5}, at=T1),
            response("m3", SONNET_ID, MAIN_USAGE, at=T2),
            response("m4", SONNET_ID, MAIN_USAGE, at=T3),
        )
        session.render()

        state = claude_statusline._read_state(
            claude_statusline._state_path(session.state_dir, session.session_id)
        )
        assert state["files"][str(session.transcript)]["models"] == [
            [epoch(T0), "claude-opus-4-8"],
            [epoch(T2), "claude-sonnet-5"],
        ]
        # Only the main transcript carries the timeline.
        assert state["files"][str(session.subagent("a1"))]["models"] == []

    def test_first_prompt_baseline_follows_a_later_user_switch(self, session):
        assert session.render(OPUS, baseline_session_start=True) == "Smart routing on"
        # The router kept Opus for the first answer, so the switch to Sonnet after it is the
        # user's `/model`: neither m2 nor the subagent beside it is routing's saving (measuring
        # both against the start model would claim $0.23).
        append(
            session.transcript,
            response("m1", OPUS_ID, MAIN_USAGE, at=T0),
            response("m2", SONNET_ID, MAIN_USAGE, at=T1),
        )
        append(session.subagent("a1"), response("s1", SONNET_ID, SUBAGENT_USAGE, at=T2))

        assert session.render(SONNET, baseline_session_start=True) == "Smart routing on"

    def test_first_prompt_routing_keeps_its_credit_after_a_user_switch(self, session):
        assert session.render(OPUS, baseline_session_start=True) == "Smart routing on"
        # The router picked Sonnet; its first answer and the subagent beside it count against Opus.
        append(session.transcript, response("m1", SONNET_ID, MAIN_USAGE, at=T0))
        append(session.subagent("a1"), response("s1", SONNET_ID, SUBAGENT_USAGE, at=T1))
        # Baseline $0.38005 - actual $0.15202.
        assert (
            session.render(SONNET, baseline_session_start=True)
            == "💰 Est. saved with smart routing: $0.23 (60%)"
        )

        # The user's `/model opus` then `/model sonnet` are their own baselines: the saving stays,
        # over a baseline that grows by $0.10507 (against Opus it would be $0.27, 52%).
        append(
            session.transcript,
            response("m2", OPUS_ID, MAIN_USAGE, at=T2),
            response("m3", SONNET_ID, MAIN_USAGE, at=T3),
        )
        assert (
            session.render(SONNET, baseline_session_start=True)
            == "💰 Est. saved with smart routing: $0.23 (47%)"
        )

    def test_discards_state_written_by_an_older_version(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"version": 1, "files": {"x": {}}}))

        assert claude_statusline._read_state(path) == {"version": claude_statusline._STATE_VERSION}

    def test_carries_the_start_model_over_from_an_older_version(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text(
            json.dumps({"version": 1, "start_model": OPUS, "key": ["x", "y"], "files": {"x": {}}})
        )

        assert claude_statusline._read_state(path) == {
            "version": claude_statusline._STATE_VERSION,
            "start_model": OPUS,
        }

    @pytest.mark.parametrize(
        "start_model", [None, "", "opus", {"display_name": "Opus"}, {"id": ""}]
    )
    def test_drops_an_invalid_start_model_from_an_older_version(self, tmp_path, start_model):
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"version": 1, "start_model": start_model}))

        assert claude_statusline._read_state(path) == {"version": claude_statusline._STATE_VERSION}

    def test_first_prompt_baseline_survives_a_state_version_change(self, session):
        state_path = claude_statusline._state_path(session.state_dir, session.session_id)
        state_path.parent.mkdir(parents=True)
        state_path.write_text(json.dumps({"version": 1, "start_model": OPUS, "files": {}}))
        append(session.transcript, response("msg-main", SONNET_ID, MAIN_USAGE))

        # The session already runs on Sonnet; re-capturing it as the start model would hide the
        # saving against Opus ($0.07505 vs $0.03002).
        assert (
            session.render(SONNET, baseline_session_start=True)
            == "💰 Est. saved with smart routing: $0.05 (60%)"
        )


class TestSessionToggle:
    """A `/smart-router` toggle rewrites the session file, which the statusLine command re-reads."""

    SAVED = "💰 Est. saved with smart routing: $0.18 (60%)"
    OFF = {ENABLE_SMART_ROUTING_ENV_VAR: "0", ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0"}

    @pytest.fixture
    def toggle(self, session, tmp_path, monkeypatch) -> Path:
        path = tmp_path / "session" / "env.json"
        path.parent.mkdir()
        monkeypatch.setenv(claude_statusline.SESSION_ENV_FILE_ENV_VAR, str(path))
        append(session.subagent("a1"), response("msg-sub", SONNET_ID, SUBAGENT_USAGE))
        return path

    def test_env_var_name_matches_the_session_env_module(self):
        assert claude_statusline.SESSION_ENV_FILE_ENV_VAR == session_env.SESSION_ENV_VAR

    @pytest.mark.parametrize(
        ("contents", "shown"),
        [
            ({}, "on"),
            (OFF, "off"),
            ({ENABLE_SMART_ROUTING_ENV_VAR: "0"}, "off"),
            ({ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1"}, "on"),
            ({ENABLE_SUBAGENT_ROUTING_ENV_VAR: " TRUE "}, "on"),
            ({ENABLE_SMART_ROUTING_ENV_VAR: "1", ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0"}, "on"),
            ({ENABLE_SUBAGENT_ROUTING_ENV_VAR: "yes"}, "off"),
            ({"UNRELATED": "0"}, "on"),
            ([], "on"),
            ("nope", "on"),
        ],
    )
    def test_the_session_file_overrides_the_launch_flag(self, session, toggle, contents, shown):
        toggle.write_text(json.dumps(contents))

        expected = self.SAVED if shown == "on" else "Smart routing off"
        assert session.render() == expected

    @pytest.mark.parametrize("damage", ["missing", "not json", ""])
    def test_an_unreadable_session_file_leaves_the_launch_flag_in_charge(
        self, session, toggle, damage
    ):
        if damage != "missing":
            toggle.write_text(damage)

        assert session.render() == self.SAVED

    def test_a_plain_launch_ignores_an_inherited_session_file(self, session, toggle):
        toggle.write_text(json.dumps({ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1"}))

        assert session.render(routing_enabled=False) == "Smart routing off"

    def test_toggling_takes_effect_on_the_next_refresh(self, session, toggle):
        toggle.write_text(json.dumps({}))
        assert session.render() == self.SAVED

        toggle.write_text(json.dumps(self.OFF))
        assert session.render() == "Smart routing off"

        toggle.write_text(json.dumps({}))
        assert session.render() == self.SAVED


class TestModUsage:
    """``--mod-usage`` prices the Claude Code mod's token sums into the mod's two segments."""

    @staticmethod
    def entry(
        agent: str,
        baseline: str | None,
        served: str,
        usage: dict,
        *,
        before_user_switch: bool = False,
    ) -> dict:
        found = {
            "agent": agent,
            "served": served,
            "before_user_switch": before_user_switch,
            "usage": usage,
        }
        return found if baseline is None else {**found, "baseline": baseline}

    @staticmethod
    def write(tmp_path: Path, entries: list, *, start_model: str | None = OPUS_ID) -> Path:
        path = tmp_path / "mod-usage.json"
        path.write_text(
            json.dumps({"version": 1, "start_model": start_model, "entries": entries}),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def price(session: Session, usage: Path, *, first_prompt: bool = False) -> dict:
        line = claude_statusline.mod_usage_output(
            usage, price_cache=session.price_cache, baseline_session_start=first_prompt
        )
        assert "\n" not in line
        return json.loads(line)

    def test_prices_tokens_against_each_entrys_baseline(self, session, tmp_path):
        entries = [
            self.entry("main", OPUS_ID, OPUS_ID, MAIN_USAGE),
            self.entry("a1", OPUS_ID, SONNET_ID, SUBAGENT_USAGE),
        ]

        # Baseline $0.38005 - actual $0.19705.
        assert self.price(session, self.write(tmp_path, entries)) == {
            "savings": "💰 Est. saved $0.18 (48%)",
            "plugin": None,
        }

    def test_an_entrys_baseline_is_the_main_model_when_its_request_ran(self, session, tmp_path):
        entries = [
            self.entry("main", OPUS_ID, OPUS_ID, MAIN_USAGE),
            self.entry("a1", OPUS_ID, SONNET_ID, SUBAGENT_USAGE),
            self.entry("main", SONNET_ID, SONNET_ID, MAIN_USAGE),
            self.entry("a1", SONNET_ID, SONNET_ID, SUBAGENT_USAGE),
        ]

        assert self.price(session, self.write(tmp_path, entries))["savings"] == (
            "💰 Est. saved $0.18 (34%)"
        )

    def test_reports_the_orchestrator_plugin_version(self, session, tmp_path, claude_config_dir):
        write_plugin_version(claude_config_dir, "0.4.4")

        # No entries yet (the mod runs the pricer at session start): just the plugin segment.
        assert self.price(session, self.write(tmp_path, [])) == {
            "savings": None,
            "plugin": "plugin v0.4.4",
        }

    def test_shows_a_net_cost_increase_honestly(self, session, tmp_path):
        entries = [
            self.entry("main", SONNET_ID, SONNET_ID, MAIN_USAGE),
            self.entry("a1", SONNET_ID, OPUS_ID, SUBAGENT_USAGE),
        ]

        # Baseline $0.152 vs actual $0.335.
        assert self.price(session, self.write(tmp_path, entries))["savings"] == (
            "cost $0.18 more (120%)"
        )

    def test_nothing_to_claim_until_a_model_was_swapped(self, session, tmp_path):
        entries = [self.entry("main", OPUS_ID, OPUS_ID, MAIN_USAGE)]

        assert self.price(session, self.write(tmp_path, entries))["savings"] is None

    def test_bills_entries_without_the_cache_write_split_as_one_hour_writes(
        self, session, tmp_path
    ):
        usage = {
            "input_tokens": 1_000,
            "cache_creation_input_tokens": 20_000,
            "output_tokens": 4_000,
        }

        # ug launches Claude with 1-hour caching, so unsplit writes bill at the 1-hour rate, as
        # SUBAGENT_USAGE does: $0.122 on Sonnet vs $0.305 at Opus rates. (5-minute: $0.092 vs $0.23.)
        assert self.price(
            session, self.write(tmp_path, [self.entry("a1", OPUS_ID, SONNET_ID, usage)])
        )["savings"] == ("💰 Est. saved $0.18 (60%)")

    def test_honors_an_explicit_cache_write_split(self, session, tmp_path):
        usage = {
            "input_tokens": 1_000,
            "cache_creation_input_tokens": 20_000,
            "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 20_000},
            "output_tokens": 4_000,
        }

        # The split says 5-minute writes: $0.092 on Sonnet vs $0.23 at Opus rates.
        assert self.price(
            session, self.write(tmp_path, [self.entry("a1", OPUS_ID, SONNET_ID, usage)])
        )["savings"] == ("💰 Est. saved $0.14 (60%)")

    def test_the_transcript_path_still_bills_unsplit_writes_as_five_minute_writes(self, session):
        usage = {
            "input_tokens": 1_000,
            "cache_creation_input_tokens": 20_000,
            "output_tokens": 4_000,
        }
        append(session.subagent("a1"), response("msg-sub", SONNET_ID, usage))

        assert session.render() == "💰 Est. saved with smart routing: $0.14 (60%)"

    def test_skips_entries_without_tokens(self, session, tmp_path):
        entries = [
            self.entry("a1", OPUS_ID, SONNET_ID, SUBAGENT_USAGE),
            self.entry("a2", "unpriced-baseline", "unpriced-served", {"input_tokens": 0}),
        ]

        assert self.price(session, self.write(tmp_path, entries))["savings"] == (
            "💰 Est. saved $0.18 (60%)"
        )

    def test_first_prompt_routing_prices_against_the_start_model(self, session, tmp_path):
        # Under first-prompt routing the router picks the main model too, so an entry's own
        # baseline (the routed model) would hide the saving; until the user switches the main
        # model its presence or absence is ignored.
        entries = [
            self.entry("main", SONNET_ID, SONNET_ID, MAIN_USAGE, before_user_switch=True),
            self.entry("a1", None, SONNET_ID, SUBAGENT_USAGE, before_user_switch=True),
        ]
        usage = self.write(tmp_path, entries, start_model=OPUS_ID)

        # Baseline $0.38 at Opus vs actual $0.15202 on Sonnet (no fixed baseline: not rerouted).
        assert self.price(session, usage, first_prompt=True)["savings"] == (
            "💰 Est. saved $0.23 (60%)"
        )
        assert self.price(session, usage)["savings"] is None

    def test_first_prompt_routing_prices_entries_after_a_user_switch_as_their_own(
        self, session, tmp_path
    ):
        entries = [
            self.entry("main", SONNET_ID, SONNET_ID, MAIN_USAGE, before_user_switch=True),
            self.entry("a1", SONNET_ID, SONNET_ID, SUBAGENT_USAGE, before_user_switch=True),
            # The user's `/model opus`, then `/model sonnet`.
            self.entry("main", OPUS_ID, OPUS_ID, MAIN_USAGE),
            self.entry("main", SONNET_ID, SONNET_ID, MAIN_USAGE),
        ]
        usage = self.write(tmp_path, entries, start_model=OPUS_ID)

        # Baseline $0.48512 - actual $0.25709, as on the transcript path.
        assert self.price(session, usage, first_prompt=True)["savings"] == (
            "💰 Est. saved $0.23 (47%)"
        )

    @pytest.mark.parametrize("start_model", [None, ""])
    def test_first_prompt_routing_without_a_start_model_has_no_estimate(
        self, session, tmp_path, start_model
    ):
        entries = [self.entry("a1", OPUS_ID, SONNET_ID, SUBAGENT_USAGE)]
        usage = self.write(tmp_path, entries, start_model=start_model)

        assert self.price(session, usage, first_prompt=True)["savings"] is None

    @pytest.mark.parametrize(
        "entries",
        [
            # An unpriced served model, or an unpriced baseline, would undercount.
            [("a1", OPUS_ID, "system.ai.claude-mystery-1")],
            [("a1", "system.ai.claude-mystery-1", SONNET_ID)],
            # Malformed entries.
            [("a1", None, SONNET_ID)],
            [("a1", "", SONNET_ID)],
            [("a1", OPUS_ID, "")],
        ],
    )
    def test_an_unpriced_or_baseline_less_entry_voids_the_estimate(
        self, session, tmp_path, entries
    ):
        good = self.entry("a0", OPUS_ID, SONNET_ID, SUBAGENT_USAGE)
        bad = [self.entry(agent, base, served, SUBAGENT_USAGE) for agent, base, served in entries]

        assert self.price(session, self.write(tmp_path, [good, *bad]))["savings"] is None

    @pytest.mark.parametrize(
        "bad",
        ["nope", 7, {"baseline": OPUS_ID, "served": SONNET_ID}, {"baseline": OPUS_ID}],
    )
    def test_a_malformed_entry_voids_the_estimate(self, session, tmp_path, bad):
        good = self.entry("a0", OPUS_ID, SONNET_ID, SUBAGENT_USAGE)

        assert self.price(session, self.write(tmp_path, [good, bad]))["savings"] is None

    def test_without_prices_there_is_no_estimate_but_the_plugin_still_shows(
        self, session, tmp_path, claude_config_dir
    ):
        write_plugin_version(claude_config_dir, "0.4.4")
        session.price_cache.unlink()
        entries = [self.entry("a1", OPUS_ID, SONNET_ID, SUBAGENT_USAGE)]

        assert self.price(session, self.write(tmp_path, entries)) == {
            "savings": None,
            "plugin": "plugin v0.4.4",
        }

    @pytest.mark.parametrize(
        "text",
        [None, "not json", "[]", json.dumps({"version": 2, "entries": []}), '{"version": 1}'],
    )
    def test_an_unusable_file_only_nulls_the_savings(
        self, session, tmp_path, claude_config_dir, text
    ):
        write_plugin_version(claude_config_dir, "0.4.4")
        usage = tmp_path / "mod-usage.json"
        if text is not None:
            usage.write_text(text, encoding="utf-8")

        assert self.price(session, usage) == {"savings": None, "plugin": "plugin v0.4.4"}

    def test_a_failing_plugin_lookup_only_nulls_the_plugin(self, session, tmp_path, monkeypatch):
        def fail() -> str:
            raise RuntimeError("plugin state unreadable")

        monkeypatch.setattr(claude_statusline, "orchestrator_plugin_version", fail)
        entries = [self.entry("a1", OPUS_ID, SONNET_ID, SUBAGENT_USAGE)]

        assert self.price(session, self.write(tmp_path, entries)) == {
            "savings": "💰 Est. saved $0.18 (60%)",
            "plugin": None,
        }

    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-P", "-m", claude_statusline.MODULE, *args],
            capture_output=True,
            timeout=30,
            check=False,
        )

    def test_cli_prints_exactly_one_utf8_json_line(self, session, tmp_path):
        entries = [self.entry("a1", OPUS_ID, SONNET_ID, SUBAGENT_USAGE)]
        usage = self.write(tmp_path, entries)

        result = self.run_cli("--mod-usage", str(usage), "--price-cache", str(session.price_cache))

        assert result.returncode == 0
        assert result.stdout.count(b"\n") == 1
        assert json.loads(result.stdout.decode("utf-8")) == {
            "savings": "💰 Est. saved $0.18 (60%)",
            "plugin": None,
        }

    def test_cli_never_fails_the_mod(self, session, tmp_path):
        result = self.run_cli(
            "--mod-usage",
            str(tmp_path / "missing.json"),
            "--price-cache",
            str(session.price_cache),
            "--baseline-session-start",
        )

        assert result.returncode == 0
        assert json.loads(result.stdout) == {"savings": None, "plugin": None}

    def test_state_dir_and_mod_usage_are_mutually_exclusive_and_one_is_required(
        self, session, tmp_path
    ):
        cache = ["--price-cache", str(session.price_cache)]

        both = self.run_cli("--state-dir", str(tmp_path), "--mod-usage", str(tmp_path), *cache)
        neither = self.run_cli(*cache)

        assert both.returncode == neither.returncode == 2


class TestSavingsText:
    def test_rounds_to_cents_and_whole_percent(self):
        assert (
            claude_statusline._savings_text(Decimal("1234.565"), Decimal("2469.13"))
            == "💰 Est. saved with smart routing: $1,234.57 (50%)"
        )

    def test_negative_reads_as_a_cost_increase(self):
        assert (
            claude_statusline._savings_text(Decimal("-0.04"), Decimal("0.40"))
            == "Smart routing cost $0.04 more (10%)"
        )

    def test_sub_cent_amounts_are_not_shown_as_zero(self):
        assert claude_statusline._savings_figures(Decimal("0.004"), Decimal("1")) == (
            "<$0.01",
            Decimal(0),
        )

    def test_the_mod_segments_share_the_rounding(self):
        assert (
            claude_statusline._mod_savings_text(Decimal("1234.565"), Decimal("2469.13"))
            == "💰 Est. saved $1,234.57 (50%)"
        )
        assert (
            claude_statusline._mod_savings_text(Decimal("-0.04"), Decimal("0.40"))
            == "cost $0.04 more (10%)"
        )


class TestOrchestratorPluginVersion:
    def test_reads_the_installed_version(self, tmp_path):
        write_plugin_version(tmp_path, "0.4.4")

        assert claude_statusline.orchestrator_plugin_version(tmp_path) == "0.4.4"

    def test_matches_regardless_of_marketplace_suffix(self, tmp_path):
        write_plugin_version(tmp_path, "1.2.3", marketplace="some-other-marketplace")

        assert claude_statusline.orchestrator_plugin_version(tmp_path) == "1.2.3"

    def test_missing_file_is_none(self, tmp_path):
        assert claude_statusline.orchestrator_plugin_version(tmp_path) is None

    def test_skips_unknown_version_and_other_plugins(self, tmp_path):
        (tmp_path / "plugins").mkdir()
        (tmp_path / "plugins" / "installed_plugins.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "plugins": {
                        "some-other-plugin@mp": [{"version": "9.9.9"}],
                        "model-orchestrator@mp": [{"version": "unknown"}],
                    },
                }
            ),
            encoding="utf-8",
        )

        assert claude_statusline.orchestrator_plugin_version(tmp_path) is None

    def test_unexpected_shape_is_none(self, tmp_path):
        (tmp_path / "plugins").mkdir()
        (tmp_path / "plugins" / "installed_plugins.json").write_text("[]", encoding="utf-8")

        assert claude_statusline.orchestrator_plugin_version(tmp_path) is None

    def test_honors_claude_config_dir_env(self, tmp_path, monkeypatch):
        write_plugin_version(tmp_path, "0.5.0")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))

        assert claude_statusline.orchestrator_plugin_version() == "0.5.0"


class TestStatusLineSetting:
    def test_wraps_the_highest_precedence_statusline(self, tmp_path):
        user = tmp_path / "home" / "settings.json"
        user.parent.mkdir()
        user.write_text(json.dumps({"statusLine": {"type": "command", "command": "user"}}))
        project = tmp_path / "project"
        (project / ".claude").mkdir(parents=True)
        (project / ".claude" / "settings.json").write_text(
            json.dumps({"statusLine": {"type": "command", "command": "project"}})
        )
        (project / ".claude" / "settings.local.json").write_text(
            json.dumps({"statusLine": {"type": "command", "command": "local"}})
        )

        def effective(settings):
            status_line = claude_statusline.effective_status_line(
                settings, user_settings_path=user, project_dir=project
            )
            return status_line and status_line["command"]

        assert effective({"statusLine": {"type": "command", "command": "caller"}}) == "caller"
        assert effective({}) == "local"
        (project / ".claude" / "settings.local.json").unlink()
        assert effective({}) == "project"
        (project / ".claude" / "settings.json").unlink()
        assert effective({"statusLine": {"type": "command", "command": " "}}) == "user"
        user.unlink()
        assert effective({}) is None

    def test_runs_only_the_status_row_without_a_user_statusline(self, tmp_path):
        setting = claude_statusline.savings_status_line(
            None,
            python="/opt/ug python/bin/python",
            state_dir=tmp_path / "state",
            price_cache=tmp_path / "prices.json",
            routing_enabled=True,
            baseline_session_start=True,
        )

        assert setting == {
            "type": "command",
            "command": shlex.join(
                [
                    "/opt/ug python/bin/python",
                    "-P",
                    "-m",
                    claude_statusline.MODULE,
                    "--state-dir",
                    str(tmp_path / "state"),
                    "--price-cache",
                    str(tmp_path / "prices.json"),
                    "--routing-enabled",
                    "--baseline-session-start",
                ]
            ),
        }

    @pytest.mark.parametrize("first_prompt", [False, True])
    def test_mod_pricer_argv_runs_the_module_on_the_same_python(self, tmp_path, first_prompt):
        argv = claude_statusline.mod_pricer_argv(
            python="/opt/ug python/bin/python",
            price_cache=tmp_path / "prices.json",
            baseline_session_start=first_prompt,
        )

        assert argv == [
            "/opt/ug python/bin/python",
            "-P",
            "-m",
            claude_statusline.MODULE,
            "--price-cache",
            str(tmp_path / "prices.json"),
            *(["--baseline-session-start"] if first_prompt else []),
        ]

    def test_mod_contract_names(self):
        # typescript/claude-mods/smart-routing-savings.ts reads both names.
        assert claude_statusline.PRICER_ENV_VAR == "UCODE_SAVINGS_PRICER"
        assert claude_statusline.MOD_USAGE_FILENAME == "mod-usage.json"

    def test_routing_disabled_omits_routing_enabled_flag(self, tmp_path):
        setting = claude_statusline.savings_status_line(
            None,
            python="/opt/ug python/bin/python",
            state_dir=tmp_path / "state",
            price_cache=tmp_path / "prices.json",
            routing_enabled=False,
            baseline_session_start=False,
        )

        assert "--routing-enabled" not in setting["command"]
        assert f"-m {claude_statusline.MODULE}" in setting["command"]

    def test_keeps_the_user_statusline_render_options(self, tmp_path):
        setting = claude_statusline.savings_status_line(
            {"type": "command", "command": "echo hi", "padding": 2, "refreshInterval": 5},
            python=sys.executable,
            state_dir=tmp_path,
            price_cache=tmp_path / "prices.json",
            routing_enabled=False,
            baseline_session_start=False,
        )

        assert setting["padding"] == 2
        assert setting["refreshInterval"] == 5
        assert "echo hi" in setting["command"]


@pytest.mark.parametrize("shell", [shell for shell in ("sh", "bash", "zsh") if shutil.which(shell)])
class TestWrappedCommand:
    """Run the generated statusLine command the way Claude Code does, through a real shell."""

    @staticmethod
    def run(shell: str, session: Session, original: str) -> str:
        setting = claude_statusline.savings_status_line(
            {"type": "command", "command": original},
            python=sys.executable,
            state_dir=session.state_dir,
            price_cache=session.price_cache,
            routing_enabled=True,
            baseline_session_start=False,
        )
        result = subprocess.run(
            [shell, "-c", setting["command"]],
            input=session.payload(OPUS),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=True,
        )
        return result.stdout

    def test_prints_the_user_row_then_the_status_row(self, shell, session):
        append(session.subagent("a1"), response("msg-sub", SONNET_ID, SUBAGENT_USAGE))

        # The user's command reads the same payload and prints without a trailing newline.
        original = 'sed -n \'s/.*"display_name": *"\\([^"]*\\)".*/\\1/p\' | tr -d \'\\n\''
        output = self.run(shell, session, original)

        assert output == ("Opus 4.8\n💰 Est. saved with smart routing: $0.18 (60%)\n")

    def test_an_exit_in_the_user_command_does_not_skip_the_row(self, shell, session):
        append(session.subagent("a1"), response("msg-sub", SONNET_ID, SUBAGENT_USAGE))

        output = self.run(shell, session, "printf 'base\\n\\n'; exit 3")

        assert output == ("base\n💰 Est. saved with smart routing: $0.18 (60%)\n")

    def test_empty_user_output_leaves_only_the_status_row(self, shell, session):
        append(session.subagent("a1"), response("msg-sub", SONNET_ID, SUBAGENT_USAGE))

        assert self.run(shell, session, "true") == (
            "💰 Est. saved with smart routing: $0.18 (60%)\n"
        )


class TestStartupCost:
    def test_statusline_imports_stay_lightweight(self):
        heavy = [
            "rich",
            "typer",
            "databricks",
            "tomlkit",
            "urllib.request",
            "ucode.config_io",
            "ucode.smart_routing.routing",
            "ucode.smart_routing.session_env",
        ]
        result = subprocess.run(
            [
                sys.executable,
                "-P",
                "-c",
                f"import json, sys; import {claude_statusline.MODULE}; "
                f"print(json.dumps([name for name in {heavy!r} if name in sys.modules]))",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )

        assert json.loads(result.stdout) == []


class TestPruneState:
    def test_removes_only_stale_files(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        stale = state_dir / "old.json"
        fresh = state_dir / "new.json"
        leftover = state_dir / ".new.json.abc.tmp"
        for path in (stale, fresh, leftover):
            path.write_text("{}")
        now = fresh.stat().st_mtime
        os.utime(stale, (now - claude_statusline.STATE_RETENTION_SECONDS - 1,) * 2)
        os.utime(leftover, (now - claude_statusline.STATE_RETENTION_SECONDS - 1,) * 2)

        claude_statusline.prune_state(state_dir, now=now)

        assert sorted(path.name for path in state_dir.iterdir()) == ["new.json"]

    def test_missing_state_dir_is_fine(self, tmp_path):
        claude_statusline.prune_state(tmp_path / "missing")
