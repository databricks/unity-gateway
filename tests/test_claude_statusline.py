"""Tests for the Claude Code smart-routing statusline row."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from ucode.smart_routing import claude_statusline, pricing
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


def response(message_id: str, model: str, usage: dict, block: str = "text") -> dict:
    return {
        "type": "assistant",
        "uuid": f"{message_id}-{block}",
        "message": {"id": message_id, "model": model, "usage": usage, "content": [{"type": block}]},
    }


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
        assert (
            session.render()
            == "💰 Est. saved with smart routing: $0.18 (48%) · /smart-router savings for details"
        )

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
        assert (
            session.render()
            == "💰 Est. saved with smart routing: $0.24 (80%) · /smart-router savings for details"
        )

    def test_counts_a_response_split_across_records_once(self, session):
        append(session.subagent("a1"), response("msg-sub", SONNET_ID, SUBAGENT_USAGE))
        once = session.render()
        append(session.subagent("a2"), response("msg-other", SONNET_ID, SUBAGENT_USAGE, "text"))
        append(session.subagent("a2"), response("msg-other", SONNET_ID, SUBAGENT_USAGE, "tool_use"))

        assert (
            once
            == "💰 Est. saved with smart routing: $0.18 (60%) · /smart-router savings for details"
        )
        assert (
            session.render()
            == "💰 Est. saved with smart routing: $0.37 (60%) · /smart-router savings for details"
        )

    def test_reads_transcripts_incrementally(self, session):
        append(session.subagent("a1"), response("msg-sub", SONNET_ID, SUBAGENT_USAGE))
        first = session.render()
        append(
            session.subagent("a1"),
            response("msg-late", SONNET_ID, SUBAGENT_USAGE),
            trailing_newline=False,
        )

        assert (
            first
            == "💰 Est. saved with smart routing: $0.18 (60%) · /smart-router savings for details"
        )
        # A line Claude Code is still writing is left for the next refresh.
        assert session.render() == first
        with session.subagent("a1").open("a", encoding="utf-8") as handle:
            handle.write("\n")
        assert (
            session.render()
            == "💰 Est. saved with smart routing: $0.37 (60%) · /smart-router savings for details"
        )

    def test_starts_over_when_a_transcript_is_rewritten(self, session):
        append(
            session.subagent("a1"),
            *(response(f"m{i}", SONNET_ID, SUBAGENT_USAGE) for i in range(3)),
        )
        assert (
            session.render()
            == "💰 Est. saved with smart routing: $0.55 (60%) · /smart-router savings for details"
        )
        session.subagent("a1").write_text(
            json.dumps(response("m0", SONNET_ID, SUBAGENT_USAGE)) + "\n"
        )

        assert (
            session.render()
            == "💰 Est. saved with smart routing: $0.18 (60%) · /smart-router savings for details"
        )

    def test_shows_a_net_cost_increase_honestly(self, session):
        append(session.transcript, response("msg-main", SONNET_ID, MAIN_USAGE))
        append(session.subagent("a1"), response("msg-sub", OPUS_ID, SUBAGENT_USAGE))

        # Baseline $0.152 (all at Sonnet) vs actual $0.335: routing cost $0.183 more.
        assert (
            session.render(SONNET)
            == "Smart routing cost $0.18 more (120%) · /smart-router savings for details"
        )

    def test_baseline_follows_the_main_model_the_user_chose(self, session):
        append(session.subagent("a1"), response("msg-sub", SONNET_ID, SUBAGENT_USAGE))

        assert (
            session.render(OPUS)
            == "💰 Est. saved with smart routing: $0.18 (60%) · /smart-router savings for details"
        )
        # Nothing ran on a model other than a Sonnet baseline, so there is nothing to claim yet.
        assert session.render(SONNET) == "Smart routing on"

    def test_first_prompt_routing_uses_the_pre_routing_model(self, session):
        # The first refresh happens before the first prompt is routed: nothing to claim yet.
        assert session.render(OPUS, baseline_session_start=True) == "Smart routing on"
        append(session.transcript, response("msg-main", SONNET_ID, MAIN_USAGE))

        # Main-agent tokens the router moved to Sonnet count toward savings too:
        # $0.07505 at Opus vs 10*2 + 100k*0.2 + 1k*10 = $0.03002 at Sonnet.
        assert (
            session.render(SONNET, baseline_session_start=True)
            == "💰 Est. saved with smart routing: $0.05 (60%) · /smart-router savings for details"
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

        assert (
            before
            == "💰 Est. saved with smart routing: $0.18 (60%) · /smart-router savings for details"
        )
        assert (
            session.render()
            == "💰 Est. saved with smart routing: <$0.01 (0%) · /smart-router savings for details"
        )

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
            "💰 Est. saved with smart routing: $0.18 (60%)"
            " · smart router plugin v0.4.4"
            " · /smart-router savings for details"
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

        assert (
            session.render()
            == "💰 Est. saved with smart routing: $0.18 (60%) · /smart-router savings for details"
        )

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

        assert output == (
            "Opus 4.8\n"
            "💰 Est. saved with smart routing: $0.18 (60%) · /smart-router savings for details\n"
        )

    def test_an_exit_in_the_user_command_does_not_skip_the_row(self, shell, session):
        append(session.subagent("a1"), response("msg-sub", SONNET_ID, SUBAGENT_USAGE))

        output = self.run(shell, session, "printf 'base\\n\\n'; exit 3")

        assert output == (
            "base\n"
            "💰 Est. saved with smart routing: $0.18 (60%) · /smart-router savings for details\n"
        )

    def test_empty_user_output_leaves_only_the_status_row(self, shell, session):
        append(session.subagent("a1"), response("msg-sub", SONNET_ID, SUBAGENT_USAGE))

        assert self.run(shell, session, "true") == (
            "💰 Est. saved with smart routing: $0.18 (60%) · /smart-router savings for details\n"
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
