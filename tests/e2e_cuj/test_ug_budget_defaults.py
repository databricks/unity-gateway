"""Live model-selection check for a fixed workspace budget recommendation."""

import re
import tomllib
from decimal import ROUND_HALF_UP, Decimal

import pytest
from base import BaseCujTest
from utils.terminal import AgentTerminal

pytestmark = [pytest.mark.cuj, pytest.mark.tui]


class TestCujBudgetDefaults(BaseCujTest):
    WORKSPACE_URL = "https://dbc-497adeef-62c0.cloud.databricks.com"
    BUDGET_ID = "25c4ce5c-fcd6-4d00-8266-69030cc1a236"
    SOL_MODEL = "system.ai.gpt-5-6-sol"
    LUNA_MODEL = "system.ai.gpt-5-6-luna"

    def test_bare_ug_uses_luna_budget_recommendation(self, live_session):
        """Scenario: launch bare ``ug`` with the fixed 2% Luna budget tier active.

        Expected: the read-only `usage` command reports the backend's spend and threshold, then
        the real backend recommends Codex/Luna and the Codex TUI selects Luna over its managed Sol
        default, without budget writes or an inference task.
        """
        session = live_session
        config_path = "/api/ai-gateway/v2/coding-agent-configs"
        payload = self.workspace.api_client.do("GET", path=config_path)
        config = payload["coding_agent_configs"][0]
        assert config["default_agent"] == "CODING_AGENT_CLAUDE_CODE", config
        assert config["smart_defaults"]["budget_id"] == self.BUDGET_ID, config
        assert config["smart_defaults"]["tiers"] == [
            {
                "spending_percentage": 0.02,
                "recommended_agent": "CODING_AGENT_CODEX",
                "recommended_model": self.LUNA_MODEL,
            }
        ], config

        session.run(
            "configure",
            "--workspace",
            self.WORKSPACE_URL,
            "--skip-upgrade",
            "--disable-databricks-ai-tools",
            timeout=240,
        )
        model_config = session.home / ".codex" / "ucode.config.toml"
        assert tomllib.loads(model_config.read_text())["model"] == self.SOL_MODEL

        before_usage = self.workspace.api_client.do(
            "POST", path=config_path + ":recommendModel", body={}
        )
        usage = session.run("usage", timeout=120)
        after_usage = self.workspace.api_client.do(
            "POST", path=config_path + ":recommendModel", body={}
        )
        for recommendation in (before_usage, after_usage):
            assert recommendation.get("recommended_agent") == "CODING_AGENT_CODEX", recommendation
            assert recommendation.get("recommended_model") == self.LUNA_MODEL, recommendation

        match = re.search(
            r"Budget spend:\s*\$([\d,]+\.\d{2})\s+of\s+\$([\d,]+\.\d{2})\s+\((\d+)%\)",
            session.redact(usage.stdout + usage.stderr),
        )
        assert match, usage.stdout + usage.stderr
        displayed_spend, displayed_threshold, displayed_percent = match.groups()
        displayed_spend = Decimal(displayed_spend.replace(",", ""))
        displayed_threshold = Decimal(displayed_threshold.replace(",", ""))
        cent = Decimal("0.01")

        before_spend = Decimal(str(before_usage["current_spend"]))
        after_spend = Decimal(str(after_usage["current_spend"]))
        before_threshold = Decimal(str(before_usage["effective_threshold"]))
        after_threshold = Decimal(str(after_usage["effective_threshold"]))
        assert before_spend <= after_spend, (before_usage, after_usage)
        assert before_threshold == after_threshold, (before_usage, after_usage)
        assert displayed_threshold == before_threshold.quantize(cent, rounding=ROUND_HALF_UP)
        assert (
            before_spend.quantize(cent, rounding=ROUND_HALF_UP)
            <= displayed_spend
            <= after_spend.quantize(cent, rounding=ROUND_HALF_UP)
        ), (before_usage, after_usage, displayed_spend)
        assert (
            int(format(float(before_spend / before_threshold), ".0%").removesuffix("%"))
            <= int(displayed_percent)
            <= int(format(float(after_spend / after_threshold), ".0%").removesuffix("%"))
        ), (before_usage, after_usage, usage)

        with AgentTerminal(session, "codex", [str(session.binary)], "budget-luna") as tui:
            tui.boot(timeout=240)
            assert tomllib.loads(model_config.read_text())["model"] == self.LUNA_MODEL
            tui.wait_for(
                lambda text: re.search(rf"model:\s+{re.escape(self.LUNA_MODEL)}\s", text),
                "Codex's selected model to be Luna",
            )
            tui.exit_normally()
